"""Optional keyed research source. No orders, no strategy calls, no credential output."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from btc_monitor.archive.exchanges import PublicRest, instruments
from btc_monitor.archive.history import HistoryLoader, validate_range
from btc_monitor.archive.types import Instrument, Observation, decimal, utc
from btc_monitor.archive.units import UNIT_RECHECK_AFTER, proof_valid, unit_proofs

INTERVALS = {"1min": 60, "5min": 300, "1hour": 3600, "4hour": 14400, "daily": 86400}
ENDPOINTS = {
    "exchanges",
    "spot-markets",
    "future-markets",
    "ohlcv-history",
    "open-interest-history",
    "funding-rate-history",
    "liquidation-history",
    "funding-rate",
}
MARKET_FIELDS = {
    "symbol",
    "exchange",
    "symbol_on_exchange",
    "base_asset",
    "quote_asset",
    "is_perpetual",
    "margined",
    "expire_at",
    "oi_lq_vol_denominated_in",
    "has_long_short_ratio_data",
    "has_ohlcv_data",
    "has_buy_sell_data",
}


class CoinalyzeError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        self.reason, self.status = reason, status
        super().__init__(reason if status is None else f"{reason} (HTTP {status})")


class CoinalyzeClient:
    def __init__(self, settings, client: httpx.AsyncClient | None = None):
        if not settings.coinalyze_api_key:
            raise CoinalyzeError("missing_api_key")
        self._key = settings.coinalyze_api_key
        self.budget = settings.coinalyze_requests_per_minute
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5))
        self._calls: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        self.request_count = 0
        self.symbol_cost = 0
        # Even LOG_LEVEL=DEBUG must not expose outgoing authentication headers.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def _reserve(self, cost: int):
        if not 1 <= cost <= self.budget:
            raise CoinalyzeError("request_exceeds_local_budget")
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0][0] >= 60:
                    self._calls.popleft()
                delay = max(0, self._blocked_until - now)
                if sum(c for _, c in self._calls) + cost > self.budget:
                    delay = max(delay, self._calls[0][0] + 60 - now)
                if delay <= 0:
                    self._calls.append((now, cost))
                    self.request_count += 1
                    self.symbol_cost += cost
                    return
                await asyncio.sleep(delay + 0.01)

    async def get(self, endpoint: str, params: dict | None = None) -> list[dict]:
        params = dict(params or {})
        if endpoint not in ENDPOINTS or set(params) - {
            "symbols",
            "interval",
            "from",
            "to",
            "convert_to_usd",
        }:
            raise CoinalyzeError("unsupported_request")
        symbols = params.get("symbols", "")
        cost = len(symbols.split(",")) if symbols else 1
        if cost > 20:
            raise CoinalyzeError("too_many_symbols")
        for attempt in range(3):
            await self._reserve(cost)
            try:
                # Fixed trusted HTTPS origin; keys are NEVER placed in query strings.
                response = await asyncio.wait_for(
                    self.client.get(
                        f"https://api.coinalyze.net/v1/{endpoint}",
                        params=params,
                        headers={"api_key": self._key.get_secret_value()},
                        follow_redirects=False,
                    ),
                    25,
                )
            except (httpx.HTTPError, TimeoutError):
                raise CoinalyzeError("transport_failure") from None
            status = response.status_code
            if status == 429:
                try:
                    wait = max(1, float(response.headers.get("Retry-After", "60")))
                    if not decimal(wait).is_finite():
                        wait = 60
                except (ValueError, ArithmeticError):
                    wait = 60
                self._blocked_until = max(self._blocked_until, time.monotonic() + wait)
                if attempt < 2:
                    continue
            elif status in {500, 502, 503, 504} and attempt < 2:
                await asyncio.sleep(2**attempt)
                continue
            if status != 200:
                reason = "authentication_failed" if status in {401, 403} else "request_failed"
                raise CoinalyzeError(reason, status)
            try:
                data = response.json(parse_float=Decimal)
            except (ValueError, ArithmeticError):
                raise CoinalyzeError("invalid_json") from None
            if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
                raise CoinalyzeError("invalid_response")
            return data
        raise CoinalyzeError("request_failed")

    async def close(self):
        await self.client.aclose()


@dataclass(frozen=True)
class ProviderMarket:
    instrument: Instrument
    provider_symbol: str
    metadata: dict
    volume_unit: str


def select_markets(exchanges, spot, futures, settings):
    """Join metadata, never guess Coinalyze's exchange suffixes or expand BTC coverage silently."""
    names = {}
    for row in exchanges:
        name = str(row.get("name", "")).casefold().replace(" ", "")
        for venue in ("bybit", "binance", "okx", "coinbase"):
            if name == venue or venue == "coinbase" and name in {"coinbasepro", "coinbaseexchange"}:
                names[row["code"]] = venue
    expected = instruments(settings.archive_venues)
    result, missing = [], []
    for inst in expected:
        candidates = []
        for row in spot if inst.market == "spot" else futures:
            if (
                names.get(row.get("exchange")) != inst.venue
                or row.get("base_asset") != "BTC"
                or row.get("quote_asset") != inst.quote
                or row.get("symbol_on_exchange") != inst.symbol
            ):
                continue
            if inst.market == "linear" and not (
                row.get("is_perpetual") is True and row.get("margined") == "STABLE"
            ):
                continue
            candidates.append(row)
        if len(candidates) != 1:
            missing.append(inst.key)
            continue
        row = {k: v for k, v in candidates[0].items() if k in MARKET_FIELDS}
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not 0 < len(symbol) <= 64 or "," in symbol:
            raise CoinalyzeError("invalid_market_symbol")
        unit = (
            settings.coinalyze_spot_volume_unit
            if inst.market == "spot"
            else {"BASE_ASSET": "base", "QUOTE_ASSET": "quote", "CONTRACTS": "contracts"}.get(
                row.get("oi_lq_vol_denominated_in"), "unknown"
            )
        )
        result.append(ProviderMarket(inst, symbol, row, unit))
    return result, missing


class CoinalyzeSource:
    def __init__(self, settings, client: CoinalyzeClient):
        self.settings, self.client = settings, client
        self.markets: list[ProviderMarket] = []
        self.missing: list[str] = []
        self.discovery_at: datetime | None = None
        self.calibration: dict = {}
        self.unit_verifications: dict = {}
        self.last_calibration_at = None
        self.funding_unit = settings.coinalyze_funding_unit

    async def discover(self):
        exchanges = await self.client.get("exchanges")
        spot = await self.client.get("spot-markets")
        futures = await self.client.get("future-markets")
        self.markets, self.missing = select_markets(exchanges, spot, futures, self.settings)
        if not self.markets:
            raise CoinalyzeError("no_matching_btc_markets")
        self.discovery_at = datetime.now(UTC)
        self.apply_verifications()
        return self.catalog()

    def restore_metadata(self, rows):
        for (feed, symbol), proof in unit_proofs(rows, datetime.now(UTC)).items():
            if any(m.instrument.key == feed and m.provider_symbol == symbol for m in self.markets):
                existing = self.unit_verifications.get(feed)
                if not existing or proof["verified_at"] > existing["verified_at"]:
                    self.unit_verifications[feed] = proof
        self.apply_verifications()

    def apply_verifications(self):
        now = datetime.now(UTC)
        for index, market in enumerate(self.markets):
            if market.instrument.market != "spot":
                continue
            proof = self.unit_verifications.get(market.instrument.key)
            valid = (
                proof
                and proof.get("provider_symbol") == market.provider_symbol
                and proof_valid(proof, now)
            )
            if proof and not valid:
                self.unit_verifications.pop(market.instrument.key)
            unit = "base" if valid else self.settings.coinalyze_spot_volume_unit
            self.markets[index] = replace(market, volume_unit=unit)

    def needs_calibration(self):
        self.apply_verifications()
        now = datetime.now(UTC)
        due = self.last_calibration_at is None or now - self.last_calibration_at >= timedelta(
            minutes=5
        )
        return due and any(
            m.instrument.market == "spot"
            and (
                m.volume_unit == "unknown"
                or m.instrument.key in self.unit_verifications
                and now
                - datetime.fromisoformat(self.unit_verifications[m.instrument.key]["verified_at"])
                >= UNIT_RECHECK_AFTER
            )
            for m in self.markets
        )

    def catalog(self):
        return {
            "selected": [
                {
                    "feed": m.instrument.key,
                    "provider_symbol": m.provider_symbol,
                    "volume_unit": m.volume_unit,
                    "has_buy_sell_data": m.metadata.get("has_buy_sell_data") is True,
                }
                for m in self.markets
            ],
            "missing_feeds": self.missing,
            "unit_verifications": self.unit_verifications,
        }

    def metadata_observation(self):
        now = datetime.now(UTC)
        return Observation(
            "coinalyze",
            "metadata",
            "BTC",
            "provider_metadata",
            0,
            now,
            now,
            now,
            "coinalyze_history",
            "coinalyze",
            "complete",
            {"provider": "coinalyze", **self.catalog(), "calibration": self.calibration},
        )

    async def calibrate(self, rest: PublicRest, *, reference_lag_minutes=10):
        """Reject unit inference unless overlapping closed bars independently agree."""
        if reference_lag_minutes not in {10, 30, 60}:
            raise ValueError("Unsupported closed reference lag")
        end = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(
            minutes=reference_lag_minutes
        )
        start = end - timedelta(minutes=5)
        self.apply_verifications()
        self.last_calibration_at = datetime.now(UTC)

        class Memory:
            def __init__(self):
                self.rows = []

            async def append(self, rows):
                self.rows.extend(rows)
                return len(rows)

        for index, market in enumerate(self.markets):
            proof = self.unit_verifications.get(market.instrument.key)
            if market.instrument.market != "spot" or market.volume_unit != "unknown" and not proof:
                continue
            if (
                proof
                and datetime.now(UTC) - datetime.fromisoformat(proof["verified_at"])
                < UNIT_RECHECK_AFTER
            ):
                continue
            evidence = {"verified": False, "start": start, "end": end}
            try:
                provider = await self.observations(market, "ohlcv-history", start, end)
                direct = Memory()
                await HistoryLoader(rest, direct).prices_and_flow(market.instrument, start, end)
                reference = {r.event_time: r for r in direct.rows}
                errors = []
                checks = []
                for row in provider:
                    other = reference.get(row.event_time)
                    if other is None:
                        continue
                    payload = other.payload
                    base = payload.get("base_volume")
                    if base is None:
                        base = payload["buy_base"] + payload["sell_base"]
                    if base <= 0:
                        continue
                    error = abs(row.payload["total_native"] - base) / base
                    volume_error = error
                    buy_error = None
                    # Binance additionally lets us verify the taker-buy field.
                    if "buy_base" in payload and row.payload["buy_native"] is not None:
                        buy_error = abs(row.payload["buy_native"] - payload["buy_base"]) / base
                        error = max(error, buy_error)
                    errors.append(error)
                    checks.append(
                        {
                            "minute": row.event_time,
                            "provider_total": row.payload["total_native"],
                            "reference_base_volume": base,
                            "volume_relative_error": volume_error,
                            "buy_relative_error": buy_error,
                        }
                    )
                verified = len(errors) >= 3 and max(errors) <= Decimal("0.000001")
                evidence.update(
                    verified=verified,
                    matched_minutes=len(errors),
                    max_relative_error=max(errors) if errors else None,
                    provider_minutes=len(provider),
                    reference_minutes=len(reference),
                    minute_checks=checks,
                )
                if verified:
                    self.markets[index] = replace(market, volume_unit="base")
                    self.unit_verifications[market.instrument.key] = {
                        "feed": market.instrument.key,
                        "provider_symbol": market.provider_symbol,
                        "verified_at": end.isoformat(),
                        "evidence": evidence.copy(),
                    }
                elif len(errors) >= 3 and min(errors) >= Decimal("0.5"):
                    # A consistent gross scale contradiction is not a transient outage.
                    self.unit_verifications.pop(market.instrument.key, None)
                    self.markets[index] = replace(market, volume_unit="unknown")
                    evidence["reason"] = "contradictory_unit_scale"
                else:
                    evidence["reason"] = (
                        "insufficient_time_matches" if len(errors) < 3 else "minute_volume_mismatch"
                    )
            except CoinalyzeError:
                raise
            except Exception as exc:
                evidence["reason"] = "direct_reference_unavailable"
                evidence["error_type"] = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    evidence["http_status"] = exc.response.status_code
            evidence["unit_verified"] = market.instrument.key in self.unit_verifications
            self.calibration[market.instrument.key] = evidence
        self.calibration["funding"] = {
            "unit": self.funding_unit,
            "basis": "official_api_schema_percent"
            if self.funding_unit == "percent"
            else "configured",
        }
        self.apply_verifications()
        return self.calibration

    @staticmethod
    def datasets(market):
        result = ["ohlcv-history"] if market.metadata.get("has_ohlcv_data", True) is True else []
        if market.instrument.market == "linear":
            result += ["open-interest-history", "funding-rate-history", "liquidation-history"]
        return result

    async def observations(self, market, endpoint, start, end, interval="1min"):
        seconds = INTERVALS[interval]
        params = {
            "symbols": market.provider_symbol,
            "interval": interval,
            "from": int(start.timestamp()),
            "to": int(end.timestamp()) - 1,
        }
        if endpoint in {"open-interest-history", "liquidation-history"}:
            params["convert_to_usd"] = "false"
        data = await self.client.get(endpoint, params)
        received = datetime.now(UTC)
        rows, seen = [], set()
        for series in data:
            if series.get("symbol") != market.provider_symbol:
                raise CoinalyzeError("unexpected_history_symbol")
            history = series.get("history")
            if not isinstance(history, list):
                raise CoinalyzeError("invalid_history")
            for bar in history:
                try:
                    event = datetime.fromtimestamp(int(bar["t"]), UTC)
                    finish = event + timedelta(seconds=seconds)
                    if not start <= event < end or finish > end or finish > received:
                        continue
                    if int(event.timestamp()) % seconds:
                        raise CoinalyzeError("unaligned_history")
                    if event in seen:
                        raise CoinalyzeError("duplicate_history_timestamp")
                    seen.add(event)
                    rows.append(self._observation(market, endpoint, bar, event, finish, received))
                except (KeyError, ValueError, TypeError, ArithmeticError, OverflowError):
                    raise CoinalyzeError("invalid_history_values") from None
        return rows

    def _observation(self, market, endpoint, bar, event, finish, received):
        inst = market.instrument
        fields = (
            ("t", "l", "s")
            if endpoint == "liquidation-history"
            else ("t", "o", "h", "l", "c", "v", "bv", "tx", "btx")
        )
        payload = {
            "provider": "coinalyze",
            "provider_symbol": market.provider_symbol,
            "market_metadata": market.metadata,
            "volume_unit": market.volume_unit,
            "quote_currency": inst.quote,
            "base_currency": "BTC",
            "raw": {k: bar[k] for k in fields if k in bar},
        }
        quality = "complete"
        if endpoint == "liquidation-history":
            long, short = decimal(bar["l"]), decimal(bar["s"])
            if min(long, short) < 0:
                raise CoinalyzeError("negative_liquidations")
            dataset = "provider_liquidations"
            payload.update(
                long_native=long,
                short_native=short,
                short_btc=short if market.volume_unit == "base" else None,
                long_btc=long if market.volume_unit == "base" else None,
            )
        else:
            o, h, low, c = (decimal(bar[k]) for k in ("o", "h", "l", "c"))
            if not low <= min(o, c) <= max(o, c) <= h:
                raise CoinalyzeError("invalid_ohlc")
            payload.update(open=o, high=h, low=low, close=c)
            if endpoint == "ohlcv-history":
                if low <= 0:
                    raise CoinalyzeError("nonpositive_price")
                total = decimal(bar["v"])
                if total < 0:
                    raise CoinalyzeError("negative_volume")
                dataset = "provider_flow"
                has_sides = market.metadata.get("has_buy_sell_data") is True and "bv" in bar
                buy = decimal(bar["bv"]) if has_sides else None
                if buy is not None and not 0 <= buy <= total:
                    raise CoinalyzeError("invalid_buy_volume")
                delta = 2 * buy - total if buy is not None else None
                payload.update(
                    total_native=total,
                    buy_native=buy,
                    sell_native=total - buy if buy is not None else None,
                    delta_native=delta,
                    delta_btc=delta if market.volume_unit == "base" else None,
                    normalization_complete=delta is not None and market.volume_unit == "base",
                )
                if not has_sides:
                    quality = "partial"
            elif endpoint == "open-interest-history":
                if low < 0:
                    raise CoinalyzeError("negative_oi")
                dataset = "provider_oi"
                payload.update(
                    reported_oi_native=c,
                    reported_oi_btc=c if market.volume_unit == "base" else None,
                    oi_side_basis="provider_basis_unverified",
                    measurement_at=finish,
                )
            else:
                dataset = "provider_funding"
                unit = self.funding_unit
                payload.update(
                    rate_native=c,
                    rate_unit=unit,
                    rate_fraction=c / 100
                    if unit == "percent"
                    else c
                    if unit == "fraction"
                    else None,
                    rate_kind="reported_history_close",
                    interval_hours=None,
                    measurement_at=finish,
                )
        return Observation(
            inst.venue,
            inst.market,
            inst.symbol,
            dataset,
            int((finish - event).total_seconds()),
            event,
            finish,
            received,
            "coinalyze_history",
            "coinalyze",
            quality,
            payload,
        )

    async def backfill(self, store, start, end, interval="1min"):
        validate_range(start, end)
        start, end = utc(start), utc(end)
        seconds = INTERVALS[interval]
        if int(start.timestamp()) % seconds or int(end.timestamp()) % seconds:
            raise CoinalyzeError("range_must_align_to_interval")
        if not self.markets:
            await self.discover()
        reports = []
        for market in self.markets:
            for endpoint in self.datasets(market):
                cursor, returned, inserted = start, 0, 0
                while cursor < end:
                    page_end = min(end, cursor + timedelta(seconds=seconds * 1000))
                    rows = await self.observations(market, endpoint, cursor, page_end, interval)
                    returned += len(rows)
                    inserted += await store.append(rows)
                    cursor = page_end
                reports.append(
                    {
                        "feed": market.instrument.key,
                        "endpoint": endpoint,
                        "interval": interval,
                        "returned": returned,
                        "inserted": inserted,
                        "expected_bars": int((end - start).total_seconds() / seconds),
                    }
                )
        return {"series": reports, "missing_feeds": self.missing}
