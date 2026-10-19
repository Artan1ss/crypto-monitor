"""Local display feed: public prices + fixed-cohort provider metrics, never signals.

Database persistence is best-effort and cannot block price delivery. Retrospective
hourly metrics are explicitly NOT historical point-in-time strategy decisions.
"""

import asyncio
import copy
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from btc_monitor.archive.coinalyze import CoinalyzeClient, CoinalyzeError, CoinalyzeSource
from btc_monitor.archive.exchanges import PublicRest
from btc_monitor.hourly.features import build_hour, universe


def boundary(at):
    return at.replace(minute=0, second=0, microsecond=0)


def parse_prices(payload, now):
    result = payload["result"]
    if result.get("symbol") != "BTCUSDT" or result.get("category") != "linear":
        raise ValueError("Unexpected market")
    values = {}
    for raw in result["list"]:
        start = datetime.fromtimestamp(int(raw[0]) / 1000, UTC)
        o, h, low, c, volume = map(float, raw[1:6])
        if (
            start.timestamp() % 3600
            or start > now
            or start in values
            or not all(math.isfinite(v) for v in (o, h, low, c, volume))
            or not 0 < low <= min(o, c) <= max(o, c) <= h
            or volume < 0
        ):
            raise ValueError("Invalid hourly candle")
        values[start] = {
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            "price": {"open": o, "high": h, "low": low, "close": c},
            "price_volume_btc": volume,
            "closed": start + timedelta(hours=1) <= now,
        }
    if not values or max(values) != boundary(now):
        raise ValueError("Stale hourly market")
    return [values[t] for t in sorted(values)]


def display_metrics(observations, now):
    # IDs here identify rows within this display calculation, never database records.
    rows = [
        {**o.values(), "id": i + 1, "available_at": o.received_at}
        for i, o in enumerate(observations)
    ]
    metadata = [r for r in rows if r["dataset"] == "provider_metadata"]
    times = sorted({r["event_time"] for r in rows if r["dataset"] == "provider_flow"})
    result = {}
    for start in times:
        if start + timedelta(hours=1) > now:
            continue
        relevant = [
            r
            for r in rows
            if r["dataset"] != "provider_metadata"
            and start <= r["end_time"] <= start + timedelta(hours=1)
        ]
        b = build_hour(relevant + metadata, start, now, "coinalyze")
        b.update(
            causal=False,
            display_only=True,
            strategy_eligible=False,
            input_ids=[],
            view_generated_at=now.isoformat(),
            lineage_kind="display_calculation_only",
        )
        result[b["start"]] = b
    return result


class HourlyMarketFeed:
    def __init__(self, settings, store=None):
        self.settings, self.store = settings, store
        self.rest = PublicRest(settings)
        self.client = CoinalyzeClient(settings) if settings.coinalyze_api_key else None
        self.source = CoinalyzeSource(settings, self.client) if self.client else None
        self.prices, self.metrics, self.raw = [], {}, {}
        self.price_received_at = self.provider_received_at = None
        self.price_error = self.provider_error = self.storage_error = None
        self.price_generation = self.provider_generation = 0
        self.tasks = []
        self.price_lock = asyncio.Lock()

    async def start(self):
        if self.tasks:
            return
        self.tasks = [
            asyncio.create_task(self._loop(self.update_prices, 30)),
            asyncio.create_task(self._loop(self.update_provider, 120)),
        ]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await self.rest.close()
        if self.client:
            await self.client.close()

    async def _loop(self, update, seconds):
        while True:
            try:
                await update()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never put exception text (which may include authentication data) into health.
                code = exc.reason if isinstance(exc, CoinalyzeError) else type(exc).__name__
                if update == self.update_prices:
                    self.price_error = code
                else:
                    self.provider_error = code
            await asyncio.sleep(seconds)

    async def update_prices(self):
        async with self.price_lock:
            # A simultaneous first API request and startup task must share a single fetch.
            if self.price_received_at and datetime.now(UTC) - self.price_received_at < timedelta(
                seconds=10
            ):
                return
            p = await self.rest.get(
                "bybit",
                "/v5/market/kline",
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "interval": "60",
                    "limit": 169,
                },
            )
            now = datetime.now(UTC)
            values = parse_prices(p, now)
            self.prices, self.price_received_at, self.price_error = values, now, None
            self.price_generation += 1

    async def persist(self, observations):
        if not self.store:
            return
        try:
            for i in range(0, len(observations), 200):
                await asyncio.wait_for(self.store.append(observations[i : i + 200]), timeout=4)
            self.storage_error = None
        except Exception as exc:
            self.storage_error = type(exc).__name__

    async def update_provider(self):
        if self.source is None:
            raise CoinalyzeError("missing_api_key")
        source = self.source
        if source.discovery_at is None or datetime.now(UTC) - source.discovery_at > timedelta(
            days=1
        ):
            await source.discover()
            if self.store:
                try:
                    source.restore_metadata(await asyncio.wait_for(self.store.unit_metadata(), 3))
                except Exception as exc:
                    self.storage_error = type(exc).__name__
        if source.needs_calibration():
            # Display-only reference allows settlement time; the exact-match criterion is unchanged.
            await source.calibrate(self.rest, reference_lag_minutes=30)
            attempts = [{"lag_minutes": 30, "checks": copy.deepcopy(source.calibration)}]
            if any(
                m.instrument.market == "spot" and m.volume_unit != "base" for m in source.markets
            ):
                await source.calibrate(self.rest, reference_lag_minutes=60)
                attempts.append({"lag_minutes": 60, "checks": copy.deepcopy(source.calibration)})
        else:
            attempts = []
        expected = {i.key for i in universe("coinalyze")}
        actual = {m.instrument.key for m in source.markets}
        if actual != expected:
            raise CoinalyzeError("fixed_cohort_incomplete")
        now = datetime.now(UTC)
        end = boundary(now)
        start = end - timedelta(hours=169 if not self.metrics else 3)
        metadata = source.metadata_observation()
        observations = [
            replace(metadata, payload={**metadata.payload, "display_reference_attempts": attempts})
        ]
        # Six flows plus three OI/funding pairs: 12 symbol calls, within the shared limiter.
        for market in source.markets:
            endpoints = ["ohlcv-history"]
            if market.instrument.market == "linear":
                endpoints += ["open-interest-history", "funding-rate-history"]
            for endpoint in endpoints:
                observations.extend(
                    await source.observations(market, endpoint, start, end, "1hour")
                )
        for o in observations:
            self.raw[o.values()["fingerprint"]] = o
        cutoff = end - timedelta(hours=170)
        self.raw = {
            k: o
            for k, o in self.raw.items()
            if o.end_time >= cutoff
            and (o.dataset != "provider_metadata" or o.received_at == observations[0].received_at)
        }
        received = datetime.now(UTC)
        metrics = await asyncio.to_thread(display_metrics, list(self.raw.values()), received)
        self.metrics, self.provider_received_at, self.provider_error = metrics, received, None
        self.provider_generation += 1
        # Immutable archive only. Never save/rewrite HourlyBarRecord or evaluate a strategy.
        await self.persist(list(self.raw.values()) if self.storage_error else observations)

    def health(self, now=None):
        now = now or datetime.now(UTC)
        latest = max((b["end"] for b in self.metrics.values() if b["complete"]), default=None)
        checks = self.source.calibration if self.source else {}
        return {
            "mode": "local_live",
            "display_only": True,
            "strategy_enabled": False,
            "telegram_enabled": False,
            "price_received_at": self.price_received_at.isoformat()
            if self.price_received_at
            else None,
            "provider_received_at": self.provider_received_at.isoformat()
            if self.provider_received_at
            else None,
            "price_fresh": bool(
                self.price_received_at
                and now - self.price_received_at < timedelta(seconds=90)
                and not self.price_error
            ),
            "provider_fresh": bool(
                self.provider_received_at
                and now - self.provider_received_at < timedelta(seconds=300)
                and not self.provider_error
            ),
            "metrics_complete": latest == boundary(now).isoformat(),
            "latest_complete_metrics_end": latest,
            "price_error": self.price_error,
            "provider_error": self.provider_error,
            "storage_error": self.storage_error,
            "price_generation": self.price_generation,
            "provider_generation": self.provider_generation,
            "price_poll_seconds": 30,
            "provider_poll_seconds": 120,
            "selected_feeds": self.source.catalog()["selected"] if self.source else [],
            "unit_checks": {
                key: {
                    field: value.get(field)
                    for field in ("verified", "reason", "matched_minutes", "max_relative_error")
                }
                for key, value in checks.items()
                if key != "funding"
            },
        }

    async def view(self, hours=72):
        if not self.prices:
            await asyncio.wait_for(self.update_prices(), 20)
        now = datetime.now(UTC)
        cutoff = boundary(now) - timedelta(hours=hours - 1)
        bars = []
        for price in self.prices:
            if datetime.fromisoformat(price["start"]) < cutoff:
                continue
            metric = copy.deepcopy(self.metrics.get(price["start"], {}))
            bar = {
                "source": "coinalyze",
                "complete": False,
                "causal": False,
                "display_only": True,
                "strategy_eligible": False,
                "flows": {},
                "aggregated": {
                    m: {"complete": False, "delta_btc": None, "volume_btc": None}
                    for m in ("spot", "linear")
                },
                "oi_btc": None,
                "funding_fraction_mean": None,
                "quality_reason": "provider_metrics_pending_or_missing",
                **metric,
                **price,
            }
            bars.append(bar)
        return {
            "schema": "btc-hourly-market-v1",
            "bars": bars,
            "health": self.health(now),
            "received_at": self.price_received_at.isoformat(),
            "source": "Bybit price + Coinalyze fixed six feeds",
        }
