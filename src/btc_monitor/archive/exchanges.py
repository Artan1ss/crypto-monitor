from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from btc_monitor.archive.types import (
    Instrument,
    Observation,
    PublicTrade,
    decimal,
    timestamp_ms,
)


def instruments(venues: tuple[str, ...]) -> list[Instrument]:
    result = []
    for venue in venues:
        if venue == "coinbase":
            result.append(Instrument(venue, "spot", "BTC-USD", "USD"))
        elif venue == "okx":
            result.extend(
                [
                    Instrument(venue, "spot", "BTC-USDT"),
                    Instrument(venue, "linear", "BTC-USDT-SWAP"),
                ]
            )
        elif venue in {"bybit", "binance"}:
            result.extend(Instrument(venue, m, "BTCUSDT") for m in ("spot", "linear"))
        else:
            raise ValueError(f"Unsupported archive venue: {venue}")
    return result


def subscription(inst: Instrument, settings) -> tuple[str, dict | None]:
    if inst.venue == "bybit":
        url = settings.bybit_spot_ws_url if inst.market == "spot" else settings.bybit_linear_ws_url
        return url, {"op": "subscribe", "args": [f"publicTrade.{inst.symbol}"]}
    if inst.venue == "binance":
        url = (
            settings.binance_spot_ws_url
            if inst.market == "spot"
            else settings.binance_linear_ws_url
        )
        return f"{url.rstrip('/')}/{inst.symbol.lower()}@aggTrade", None
    if inst.venue == "okx":
        return settings.okx_public_ws_url, {
            "op": "subscribe",
            "args": [{"channel": "trades", "instId": inst.symbol}],
        }
    return settings.coinbase_ws_url, {
        "type": "subscribe",
        "product_ids": [inst.symbol],
        "channels": ["matches", "heartbeat"],
    }


def parse_message(inst: Instrument, message: dict, received: datetime) -> list[PublicTrade]:
    """Normalize taker side and base BTC size; NEVER infer sign from candle direction."""
    rows = []
    if inst.venue == "bybit" and message.get("topic") == f"publicTrade.{inst.symbol}":
        for item in message.get("data", []):
            if item["s"] != inst.symbol:
                raise ValueError("Unexpected Bybit instrument")
            rows.append(
                (
                    timestamp_ms(item["T"]),
                    str(item["i"]),
                    item["S"].lower(),
                    item["p"],
                    decimal(item["v"]),
                    None,
                )
            )
    elif inst.venue == "binance":
        item = message.get("data", message)
        if item.get("e") == "aggTrade":
            if item["s"] != inst.symbol or not isinstance(item["m"], bool):
                raise ValueError("Invalid Binance trade")
            rows.append(
                (
                    timestamp_ms(item["T"]),
                    str(item["a"]),
                    "sell" if item["m"] else "buy",
                    item["p"],
                    decimal(item["q"]),
                    int(item["a"]),
                )
            )
    elif inst.venue == "okx" and message.get("arg", {}).get("channel") == "trades":
        for item in message.get("data", []):
            if item["instId"] != inst.symbol:
                raise ValueError("Unexpected OKX instrument")
            rows.append(
                (
                    timestamp_ms(item["ts"]),
                    str(item["tradeId"]),
                    item["side"],
                    item["px"],
                    decimal(item["sz"]) * inst.size_multiplier,
                    None,
                )
            )
    elif inst.venue == "coinbase" and message.get("type") == "match":
        if message["product_id"] != inst.symbol or message["side"] not in {"buy", "sell"}:
            raise ValueError("Invalid Coinbase trade")
        # Coinbase side is MAKER side. last_match is an old seed, not a new fill.
        rows.append(
            (
                datetime.fromisoformat(message["time"].replace("Z", "+00:00")),
                str(message["trade_id"]),
                "buy" if message["side"] == "sell" else "sell",
                message["price"],
                decimal(message["size"]),
                int(message["trade_id"]),
            )
        )
    return [
        PublicTrade(inst, t, received, trade_id, side, decimal(price), size, seq)
        for t, trade_id, side, price, size, seq in rows
    ]


class PublicRest:
    """Public market data only. No account/trading endpoints or API signing."""

    def __init__(self, settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))

    def base_url(self, venue: str, market: str = "linear") -> str:
        if venue == "binance":
            return (
                self.settings.binance_spot_rest_url
                if market == "spot"
                else self.settings.binance_linear_rest_url
            )
        return getattr(self.settings, f"{venue}_rest_url")

    async def get(self, venue: str, path: str, params=None, market="linear"):
        for attempt in range(3):
            response = await asyncio.wait_for(
                self.client.get(
                    self.base_url(venue, market).rstrip("/") + path,
                    params=params,
                ),
                timeout=20,
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                try:
                    delay = min(30, max(1, float(response.headers.get("Retry-After", "2"))))
                except ValueError:
                    delay = 2
                await asyncio.sleep(delay * (attempt + 1))
                continue
            response.raise_for_status()
            result = response.json()
            if venue == "bybit" and result.get("retCode") != 0:
                raise ValueError(f"Bybit response code {result.get('retCode')}")
            if venue == "okx" and result.get("code") != "0":
                raise ValueError(f"OKX response code {result.get('code')}")
            return result
        raise RuntimeError("Public request failed")

    async def metadata(self, inst: Instrument) -> tuple[Instrument, dict]:
        if inst.venue == "okx" and inst.market == "linear":
            result = await self.get(
                "okx", "/api/v5/public/instruments", {"instType": "SWAP", "instId": inst.symbol}
            )
            info = result["data"][0]
            if info["ctType"] != "linear" or info["ctValCcy"] != "BTC":
                raise ValueError("Only linear BTC-valued OKX swaps are supported")
            multiplier = decimal(info["ctVal"]) * decimal(info.get("ctMult") or "1")
            if multiplier <= 0:
                raise ValueError("Invalid OKX contract value")
            return replace(inst, size_multiplier=multiplier), info
        if inst.venue == "bybit" and inst.market == "linear":
            result = await self.get(
                "bybit",
                "/v5/market/instruments-info",
                {"category": "linear", "symbol": inst.symbol},
            )
            return inst, result["result"]["list"][0]
        if inst.venue == "binance" and inst.market == "linear":
            result = await self.get("binance", "/fapi/v1/fundingInfo")
            info = next((r for r in result if r["symbol"] == inst.symbol), None)
            return inst, info or {"fundingIntervalHours": 8, "interval_source": "default"}
        return inst, {"size_unit": "BTC", "quote": inst.quote}

    async def derivatives(self, inst: Instrument, session: str) -> list[Observation]:
        """OI and funding use independent event times; no fake synchronous snapshot."""
        now = datetime.now(UTC)
        if inst.venue == "bybit":
            result = await self.get(
                "bybit", "/v5/market/tickers", {"category": "linear", "symbol": inst.symbol}
            )
            row = result["result"]["list"][0]
            event = timestamp_ms(result["time"])
            oi = decimal(row.get("singleOpenInterest") or row["openInterest"])
            basis = "single_side" if row.get("singleOpenInterest") else "both_sides"
            # Current official Bybit schema explicitly defines openInterest as BOTH sides.
            base_oi = oi if basis == "single_side" else oi / 2
            rate = decimal(row["fundingRate"])
            hours = decimal(row["fundingIntervalHour"]) if row.get("fundingIntervalHour") else None
            next_time = timestamp_ms(row["nextFundingTime"])
            oi_time, funding_time = event, event
            oi_raw, funding_raw = row, row
        elif inst.venue == "binance":
            oi_raw = await self.get("binance", "/fapi/v1/openInterest", {"symbol": inst.symbol})
            funding_raw = await self.get(
                "binance", "/fapi/v1/premiumIndex", {"symbol": inst.symbol}
            )
            _, info = await self.metadata(inst)
            oi, base_oi, basis = (
                decimal(oi_raw["openInterest"]),
                decimal(oi_raw["openInterest"]),
                "single_side",
            )
            oi_time, funding_time = timestamp_ms(oi_raw["time"]), timestamp_ms(funding_raw["time"])
            rate = decimal(funding_raw["lastFundingRate"])
            hours = decimal(info["fundingIntervalHours"])
            next_time = timestamp_ms(funding_raw["nextFundingTime"])
        elif inst.venue == "okx":
            oi_result = await self.get(
                "okx", "/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst.symbol}
            )
            fr_result = await self.get(
                "okx", "/api/v5/public/funding-rate", {"instId": inst.symbol}
            )
            oi_raw, funding_raw = oi_result["data"][0], fr_result["data"][0]
            oi, base_oi, basis = decimal(oi_raw["oi"]), decimal(oi_raw["oiCcy"]), "single_side"
            oi_time, funding_time = timestamp_ms(oi_raw["ts"]), timestamp_ms(funding_raw["ts"])
            rate = decimal(funding_raw["fundingRate"])
            next_time = timestamp_ms(funding_raw["fundingTime"])
            hours = (
                decimal(int(funding_raw["nextFundingTime"]) - int(funding_raw["fundingTime"]))
                / 3600000
            )
        else:
            return []
        received = datetime.now(UTC)
        common = (inst.venue, inst.market, inst.symbol)
        return [
            Observation(
                *common,
                "oi",
                0,
                oi_time,
                oi_time,
                received,
                "live",
                session,
                "complete"
                if now - timedelta(minutes=2) <= oi_time <= received + timedelta(seconds=5)
                else "partial",
                {
                    "base_oi_btc": base_oi,
                    "raw_oi": oi,
                    "raw_basis": basis,
                    "normalized_basis": "single_side",
                    "raw": oi_raw,
                },
            ),
            Observation(
                *common,
                "funding",
                0,
                funding_time,
                funding_time,
                received,
                "live",
                session,
                "complete" if hours and hours > 0 else "unknown",
                {
                    "rate": rate,
                    "interval_hours": hours,
                    "rate_per_8h": rate * 8 / hours if hours and hours > 0 else None,
                    "rate_kind": "current_reported",
                    "next_funding_time": next_time,
                    "raw": funding_raw,
                },
            ),
        ]

    async def close(self):
        await self.client.aclose()
