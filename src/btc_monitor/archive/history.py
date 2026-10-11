from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from btc_monitor.archive.exchanges import PublicRest
from btc_monitor.archive.types import Instrument, Observation, decimal, timestamp_ms, utc


def validate_range(start: datetime, end: datetime):
    start, end = utc(start), utc(end)
    if start != start.replace(second=0, microsecond=0) or end != end.replace(
        second=0, microsecond=0
    ):
        raise ValueError("Use minute-aligned UTC boundaries")
    if start >= end or end > datetime.now(UTC):
        raise ValueError("History requires start < end <= now")
    if end - start > timedelta(days=31):
        raise ValueError("Backfill jobs are bounded to 31 days; run additional explicit ranges")


def binance_bar(inst: Instrument, row: list, received: datetime, session: str) -> Observation:
    start = timestamp_ms(row[0])
    end = start + timedelta(minutes=1)
    total_base, total_quote = decimal(row[5]), decimal(row[7])
    buy_base, buy_quote = decimal(row[9]), decimal(row[10])
    if not 0 <= buy_base <= total_base or not 0 <= buy_quote <= total_quote:
        raise ValueError("Invalid Binance taker volume")
    if timestamp_ms(row[6]) != end - timedelta(milliseconds=1):
        raise ValueError("Unexpected Binance kline interval")
    return Observation(
        inst.venue,
        inst.market,
        inst.symbol,
        "flow",
        60,
        start,
        end,
        received,
        "rest_history",
        session,
        "complete",
        {
            "schema_version": 1,
            "base_currency": "BTC",
            "quote_currency": inst.quote,
            "open": decimal(row[1]),
            "high": decimal(row[2]),
            "low": decimal(row[3]),
            "close": decimal(row[4]),
            "buy_base": buy_base,
            "sell_base": total_base - buy_base,
            "delta_base": 2 * buy_base - total_base,
            "buy_quote": buy_quote,
            "sell_quote": total_quote - buy_quote,
            "delta_quote": 2 * buy_quote - total_quote,
            "record_count": int(row[8]),
            "count_semantics": "exchange_kline_trade_count",
            "raw": row,
            "quality_reasons": [],
            "coverage_scope": "exchange_closed_kline",
        },
    )


class HistoryLoader:
    def __init__(self, rest: PublicRest, store):
        self.rest, self.store = rest, store

    async def prices_and_flow(self, inst: Instrument, start: datetime, end: datetime) -> dict:
        validate_range(start, end)
        session = f"backfill:{start.isoformat()}:{end.isoformat()}"
        # IDs use max 64 characters in storage.
        session = session[:64]
        cursor, saved, returned = start, 0, 0
        while cursor < end:
            limit = 200 if inst.venue in {"bybit", "okx"} else 300
            page_end = min(end, cursor + timedelta(minutes=limit))
            start_ms = int(cursor.timestamp() * 1000)
            end_ms = int(page_end.timestamp() * 1000)
            if inst.venue == "binance":
                path = "/api/v3/klines" if inst.market == "spot" else "/fapi/v1/klines"
                raw = await self.rest.get(
                    "binance",
                    path,
                    {
                        "symbol": inst.symbol,
                        "interval": "1m",
                        "limit": limit,
                        "startTime": start_ms,
                        "endTime": end_ms - 1,
                    },
                    inst.market,
                )
            elif inst.venue == "bybit":
                result = await self.rest.get(
                    "bybit",
                    "/v5/market/kline",
                    {
                        "symbol": inst.symbol,
                        "category": inst.market,
                        "interval": "1",
                        "limit": limit,
                        "start": start_ms,
                        "end": end_ms - 1,
                    },
                )
                raw = result["result"]["list"]
            elif inst.venue == "okx":
                result = await self.rest.get(
                    "okx",
                    "/api/v5/market/history-candles",
                    {
                        "instId": inst.symbol,
                        "bar": "1m",
                        "limit": limit,
                        "after": end_ms,
                        "before": start_ms - 1,
                    },
                )
                raw = result["data"]
            else:
                raw = await self.rest.get(
                    "coinbase",
                    f"/products/{inst.symbol}/candles",
                    {"granularity": 60, "start": cursor.isoformat(), "end": page_end.isoformat()},
                )
            received, observations = datetime.now(UTC), []
            for row in raw:
                ts = (
                    datetime.fromtimestamp(int(row[0]), UTC)
                    if inst.venue == "coinbase"
                    else timestamp_ms(row[0])
                )
                bar_end = ts + timedelta(minutes=1)
                if not cursor <= ts < page_end or bar_end > received or bar_end > end:
                    continue
                if inst.venue == "okx" and str(row[8]) != "1":
                    continue
                if inst.venue == "binance":
                    observations.append(binance_bar(inst, row, received, session))
                else:
                    if inst.venue == "coinbase":
                        payload = {
                            "open": decimal(row[3]),
                            "high": decimal(row[2]),
                            "low": decimal(row[1]),
                            "close": decimal(row[4]),
                            "base_volume": decimal(row[5]),
                        }
                    else:
                        payload = dict(
                            zip(
                                ("open", "high", "low", "close"),
                                (decimal(r) for r in row[1:5]),
                                strict=True,
                            )
                        )
                        payload["base_volume"] = (
                            decimal(row[6]) if inst.venue == "okx" else decimal(row[5])
                        )
                    payload.update(
                        raw=row, base_currency="BTC", quote_currency=inst.quote, cvd_available=False
                    )
                    observations.append(
                        Observation(
                            inst.venue,
                            inst.market,
                            inst.symbol,
                            "candle",
                            60,
                            ts,
                            bar_end,
                            received,
                            "rest_history",
                            session,
                            "complete",
                            payload,
                        )
                    )
            returned += len(observations)
            saved += await self.store.append(observations)
            # Advance a bounded explicit time window even when exchange history is missing.
            cursor = page_end
            await asyncio.sleep(0.25)
        return {
            "venue": inst.venue,
            "market": inst.market,
            "returned": returned,
            "inserted": saved,
            "expected_minutes": int((end - start).total_seconds() / 60),
            "cvd_available": inst.venue == "binance",
        }

    async def derivatives(self, inst: Instrument, start: datetime, end: datetime) -> dict:
        """Historical OI (Bybit/Binance) and settled funding (three venues)."""
        validate_range(start, end)
        if inst.market != "linear":
            return {"oi_rows": 0, "funding_rows": 0}
        session, counts = "public_derivatives_backfill", {"oi_rows": 0, "funding_rows": 0}
        if inst.venue in {"bybit", "binance"}:
            cursor = start
            while cursor < end:
                page_end = min(end, cursor + timedelta(minutes=5 * 200))
                params = {
                    "symbol": inst.symbol,
                    "startTime": int(cursor.timestamp() * 1000),
                    "endTime": int(page_end.timestamp() * 1000) - 1,
                    "limit": 200,
                }
                if inst.venue == "bybit":
                    params.update(category="linear", intervalTime="5min")
                    result = await self.rest.get(inst.venue, "/v5/market/open-interest", params)
                    raw = result["result"]["list"]
                else:
                    params["period"] = "5m"
                    raw = await self.rest.get(inst.venue, "/futures/data/openInterestHist", params)
                received, rows = datetime.now(UTC), []
                for row in raw:
                    event = timestamp_ms(row["timestamp"])
                    if not start <= event < end:
                        continue
                    if inst.venue == "bybit":
                        single = row.get("singleOpenInterest")
                        base = decimal(single) if single else decimal(row["openInterest"]) / 2
                        basis = "single_side" if single else "both_sides"
                    else:
                        base, basis = decimal(row["sumOpenInterest"]), "single_side"
                    rows.append(
                        Observation(
                            inst.venue,
                            inst.market,
                            inst.symbol,
                            "oi",
                            0,
                            event,
                            event,
                            received,
                            "rest_history",
                            session,
                            "complete",
                            {
                                "base_oi_btc": base,
                                "raw_basis": basis,
                                "normalized_basis": "single_side",
                                "raw": row,
                            },
                        )
                    )
                counts["oi_rows"] += await self.store.append(rows)
                cursor = page_end
                await asyncio.sleep(0.25)
        # Settled funding has no inferred interval; retain the settlement timestamp and raw data.
        cursor_end, cursor_start = end, start
        while cursor_end > start and cursor_start < end:
            params = {
                "symbol": inst.symbol,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(cursor_end.timestamp() * 1000) - 1,
                "limit": 200,
            }
            if inst.venue == "bybit":
                params["category"] = "linear"
                result = await self.rest.get(inst.venue, "/v5/market/funding/history", params)
                raw = result["result"]["list"]
                field = "fundingRateTimestamp"
            elif inst.venue == "binance":
                params["startTime"] = int(cursor_start.timestamp() * 1000)
                raw = await self.rest.get(inst.venue, "/fapi/v1/fundingRate", params)
                field = "fundingTime"
            else:
                result = await self.rest.get(
                    "okx",
                    "/api/v5/public/funding-rate-history",
                    {
                        "instId": inst.symbol,
                        "limit": 200,
                        "after": int(cursor_end.timestamp() * 1000),
                    },
                )
                raw, field = result["data"], "fundingTime"
            received, rows = datetime.now(UTC), []
            for row in raw:
                event = timestamp_ms(row[field])
                if start <= event < end:
                    rows.append(
                        Observation(
                            inst.venue,
                            inst.market,
                            inst.symbol,
                            "funding",
                            0,
                            event,
                            event,
                            received,
                            "rest_history",
                            session,
                            "complete",
                            {
                                "rate": decimal(row.get("realizedRate") or row["fundingRate"]),
                                "rate_kind": "settled",
                                "interval_hours": None,
                                "raw": row,
                            },
                        )
                    )
            counts["funding_rows"] += await self.store.append(rows)
            if not raw:
                break
            if inst.venue == "binance":
                latest = max(timestamp_ms(row[field]) for row in raw)
                if latest < cursor_start:
                    break
                cursor_start = latest + timedelta(milliseconds=1)
                await asyncio.sleep(0.25)
                continue
            earliest = min(timestamp_ms(row[field]) for row in raw)
            if earliest >= cursor_end:
                break
            cursor_end = earliest
            await asyncio.sleep(0.25)
        return counts
