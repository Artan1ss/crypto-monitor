from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog

from btc_monitor.collectors.bybit import (
    BybitRestClient,
    BybitWebSocket,
    parse_kline,
    parse_trades,
)
from btc_monitor.collectors.flow import FlowAggregator
from btc_monitor.collectors.orderbook import LocalOrderBook
from btc_monitor.config import Settings
from btc_monitor.domain import ZERO, Candle, Direction, Market, Pivot, ServiceStatus
from btc_monitor.indicators import (
    confirmed_pivots,
    cvd_divergence,
    detect_sweep,
    ema,
    percent_change,
    wilder_atr,
)
from btc_monitor.repository import Repository
from btc_monitor.strategy import SignalEngine, StrategyContext
from btc_monitor.telegram import TelegramClient, render_signal

log = structlog.get_logger()


class MonitorRuntime:
    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository
        self.rest = BybitRestClient(settings.bybit_rest_url)
        self.telegram = TelegramClient(
            settings.telegram_enabled, settings.telegram_bot_token, settings.telegram_chat_id
        )
        self.flow = FlowAggregator(settings.symbol, repository)
        self.orderbook = LocalOrderBook()
        self.engine = SignalEngine(settings)
        self.status = ServiceStatus.STARTING
        self.started_at = datetime.now(UTC)
        self.candles_5m: list[Candle] = []
        self.candles_1h: list[Candle] = []
        self.ticker: dict = {}
        self.streams: dict[str, BybitWebSocket] = {}
        self.tasks: list[asyncio.Task] = []
        self.consumed: set[tuple[str, datetime, Decimal]] = set()
        self.last_signal_at: datetime | None = None
        self.last_candle_at: datetime | None = None
        self.last_error: str | None = None
        self._stream_down_since: dict[str, datetime] = {}
        self._stream_warned: set[str] = set()

    async def start(self) -> None:
        await self.flow.initialize()
        await self._backfill()
        warmup_counts = await self.repository.flow_counts(self.settings.symbol)
        self.status = (
            ServiceStatus.RUNNING
            if self._warmup_complete(warmup_counts)
            else ServiceStatus.WARMING_UP
        )
        spot = BybitWebSocket(
            "spot",
            self.settings.bybit_spot_ws_url,
            [f"publicTrade.{self.settings.symbol}"],
            self._handle_spot,
            self.settings.heartbeat_seconds,
            self.settings.ws_stale_seconds,
            self._stream_state,
        )
        linear_topics = [
            f"publicTrade.{self.settings.symbol}",
            f"tickers.{self.settings.symbol}",
            f"kline.5.{self.settings.symbol}",
            f"kline.60.{self.settings.symbol}",
        ]
        if self.settings.orderbook_enabled:
            linear_topics.append(f"orderbook.50.{self.settings.symbol}")
        linear = BybitWebSocket(
            "linear",
            self.settings.bybit_linear_ws_url,
            linear_topics,
            self._handle_linear,
            self.settings.heartbeat_seconds,
            self.settings.ws_stale_seconds,
            self._stream_state,
        )
        self.streams = {"spot": spot, "linear": linear}
        self.tasks = [
            asyncio.create_task(spot.run(), name="bybit-spot"),
            asyncio.create_task(linear.run(), name="bybit-linear"),
            asyncio.create_task(self._ticker_persist_loop(), name="ticker-persistence"),
            asyncio.create_task(self._health_loop(), name="runtime-health"),
        ]
        if self.settings.send_startup_message:
            message = (
                "BTC monitor online — collecting initial CVD history."
                if self.status == ServiceStatus.WARMING_UP
                else "BTC monitor online — stored CVD history restored."
            )
            await self.telegram.send(message)
        log.info("monitor_started", status=self.status.value)

    async def stop(self) -> None:
        self.status = ServiceStatus.STOPPING
        for stream in self.streams.values():
            await stream.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.flow.flush()
        await self.rest.close()
        await self.telegram.close()

    async def _backfill(self) -> None:
        try:
            self.candles_5m = await self.rest.klines(self.settings.symbol, "5", 200)
            self.candles_1h = await self.rest.klines(self.settings.symbol, "60", 250)
            atr_values = wilder_atr(self.candles_5m)
            for candle, atr in zip(self.candles_5m, atr_values, strict=True):
                await self.repository.save_candle(self.settings.symbol, candle, "5", atr14=atr)
            closes = [c.close for c in self.candles_1h]
            ema50, ema200 = ema(closes, 50), ema(closes, 200)
            for candle, value50, value200 in zip(self.candles_1h, ema50, ema200, strict=True):
                await self.repository.save_candle(
                    self.settings.symbol, candle, "60", ema50=value50, ema200=value200
                )
        except Exception as exc:
            self.last_error = f"backfill: {exc}"
            log.exception("backfill_failed")
            self.candles_5m = await self.repository.load_candles(self.settings.symbol, "5", 200)
            self.candles_1h = await self.repository.load_candles(self.settings.symbol, "60", 250)

    async def _handle_spot(self, message: dict) -> None:
        for trade in parse_trades(message, Market.SPOT):
            await self.flow.add(trade)

    async def _handle_linear(self, message: dict) -> None:
        topic = message.get("topic", "")
        if topic.startswith("publicTrade"):
            for trade in parse_trades(message, Market.LINEAR):
                await self.flow.add(trade)
        elif topic.startswith("tickers"):
            self._update_ticker(message.get("data", {}))
        elif topic.startswith("kline"):
            parsed = parse_kline(message)
            if parsed and parsed[1].confirmed:
                await self._on_candle(*parsed)
        elif topic.startswith("orderbook"):
            self.orderbook.apply(message.get("type", "delta"), message.get("data", {}))

    def _update_ticker(self, data: dict) -> None:
        mappings = {
            "lastPrice": "last_price",
            "markPrice": "mark_price",
            "indexPrice": "index_price",
            "openInterest": "open_interest",
            "openInterestValue": "open_interest_value",
            "fundingRate": "funding_rate",
        }
        for source, target in mappings.items():
            if source in data and data[source] != "":
                self.ticker[target] = Decimal(data[source])
        if data.get("nextFundingTime"):
            self.ticker["next_funding_time"] = datetime.fromtimestamp(
                int(data["nextFundingTime"]) / 1000, tz=UTC
            )
        self.ticker["timestamp"] = datetime.now(UTC)

    async def _on_candle(self, interval: str, candle: Candle) -> None:
        series = self.candles_5m if interval == "5" else self.candles_1h
        if series and series[-1].timestamp == candle.timestamp:
            series[-1] = candle
        else:
            series.append(candle)
        del series[:-300]
        if interval == "5":
            atr = wilder_atr(series)[-1]
            await self.repository.save_candle(self.settings.symbol, candle, "5", atr14=atr)
            self.last_candle_at = datetime.now(UTC)
            await self.flow.flush()
            if atr is not None:
                await self._evaluate(atr)
        else:
            closes = [c.close for c in series]
            value50, value200 = ema(closes, 50)[-1], ema(closes, 200)[-1]
            await self.repository.save_candle(
                self.settings.symbol, candle, "60", ema50=value50, ema200=value200
            )

    async def _evaluate(self, atr: Decimal) -> None:
        if self.status != ServiceStatus.RUNNING or len(self.candles_5m) < 30:
            return
        lows, highs = confirmed_pivots(
            self.candles_5m, self.settings.pivot_left, self.settings.pivot_right
        )
        current_index = len(self.candles_5m) - 1
        valid_lows = [
            p
            for p in lows
            if self.settings.pivot_min_age <= current_index - p.index <= self.settings.pivot_max_age
        ]
        valid_highs = [
            p
            for p in highs
            if self.settings.pivot_min_age <= current_index - p.index <= self.settings.pivot_max_age
        ]
        current = self.candles_5m[-1]
        for direction, pivots, opposing in (
            (Direction.LONG, valid_lows, highs),
            (Direction.SHORT, valid_highs, lows),
        ):
            if not pivots:
                continue
            pivot = pivots[-1]
            key = (direction.value, pivot.timestamp, pivot.price)
            if key in self.consumed:
                continue
            sweep = detect_sweep(current, pivot, atr, direction)
            if not sweep:
                continue
            context = await self._strategy_context(sweep, opposing)
            signal = self.engine.evaluate(context)
            if signal and self._cooldown_ready(signal.timestamp):
                inserted = await self.repository.save_signal(signal)
                if not inserted:
                    self.consumed.add(key)
                    continue
                message_id = await self.telegram.send(render_signal(signal))
                if message_id:
                    await self.repository.mark_telegram_sent(signal.signal_id, message_id)
                self.consumed.add(key)
                self.last_signal_at = signal.timestamp
                log.info(
                    "signal_created",
                    id=str(signal.signal_id),
                    score=str(signal.score),
                    direction=signal.direction.value,
                )

    async def _strategy_context(self, sweep, opposing: list[Pivot]) -> StrategyContext:
        spot_rows, linear_rows = await asyncio.gather(
            self.repository.load_flow_rows(self.settings.symbol, Market.SPOT),
            self.repository.load_flow_rows(self.settings.symbol, Market.LINEAR),
        )
        spot_delta, spot_cvd, spot_ref = self._flow_features(spot_rows, sweep.pivot.timestamp)
        linear_delta, linear_cvd, linear_ref = self._flow_features(
            linear_rows, sweep.pivot.timestamp
        )
        spot_history = self._five_minute_deltas(spot_rows)
        linear_history = self._five_minute_deltas(linear_rows)
        oi5_row, oi15_row = await asyncio.gather(
            self.repository.ticker_at(self.settings.symbol, timedelta(minutes=5)),
            self.repository.ticker_at(self.settings.symbol, timedelta(minutes=15)),
        )
        oi_current = self.ticker.get("open_interest")
        oi5 = percent_change(oi_current, oi5_row.open_interest if oi5_row else None)
        oi15 = percent_change(oi_current, oi15_row.open_interest if oi15_row else None)
        previous = self.candles_5m[-2].close
        price_change = percent_change(self.candles_5m[-1].close, previous) or ZERO
        htf_close = self.candles_1h[-1].close if self.candles_1h else None
        closes = [c.close for c in self.candles_1h]
        ema50_value = ema(closes, 50)[-1] if len(closes) >= 50 else None
        ema200_value = ema(closes, 200)[-1] if len(closes) >= 200 else None
        metrics = self.orderbook.metrics()
        return StrategyContext(
            symbol=self.settings.symbol,
            sweep=sweep,
            atr=atr_or(sweep.candle.atr14, self.candles_5m, Decimal("1")),
            spot_divergence=cvd_divergence(spot_cvd, spot_ref, spot_history, sweep.direction),
            futures_divergence=cvd_divergence(
                linear_cvd, linear_ref, linear_history, sweep.direction
            ),
            spot_delta_5m=spot_delta,
            futures_delta_5m=linear_delta,
            price_change_5m=price_change,
            oi_change_5m=oi5,
            oi_change_15m=oi15,
            funding_rate=self.ticker.get("funding_rate"),
            previous_turnovers=[c.turnover for c in self.candles_5m[-21:-1]],
            htf_close=htf_close,
            htf_ema50=ema50_value,
            htf_ema200=ema200_value,
            opposing_pivots=opposing,
            orderbook_imbalance=metrics.get("imbalance_025") if metrics else None,
        )

    @staticmethod
    def _five_minute_deltas(rows) -> list[Decimal]:
        grouped: dict[datetime, Decimal] = defaultdict(lambda: ZERO)
        for row in rows:
            bucket = row.bucket_start.replace(
                minute=(row.bucket_start.minute // 5) * 5, second=0, microsecond=0
            )
            grouped[bucket] += row.delta_notional
        return [grouped[key] for key in sorted(grouped)]

    def _flow_features(self, rows, pivot_time: datetime) -> tuple[Decimal, Decimal, Decimal | None]:
        if not rows:
            return ZERO, ZERO, None
        current = rows[-1].cumulative_cvd
        reference_candidates = [
            row for row in rows if row.bucket_start <= pivot_time + timedelta(minutes=5)
        ]
        reference = reference_candidates[-1].cumulative_cvd if reference_candidates else None
        return sum((row.delta_notional for row in rows[-5:]), ZERO), current, reference

    def _cooldown_ready(self, timestamp: datetime) -> bool:
        return self.last_signal_at is None or timestamp - self.last_signal_at >= timedelta(
            minutes=self.settings.signal_cooldown_min
        )

    async def _ticker_persist_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.ticker_persist_seconds)
            if self.ticker.get("last_price") is not None:
                await self.repository.save_ticker(self.settings.symbol, dict(self.ticker))

    async def _stream_state(self, name: str, connected: bool) -> None:
        if connected:
            if name in self._stream_warned:
                await self.telegram.send(f"✅ BTC monitor recovered: {name} stream reconnected.")
            self._stream_down_since.pop(name, None)
            self._stream_warned.discard(name)
        else:
            self._stream_down_since.setdefault(name, datetime.now(UTC))

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = datetime.now(UTC)
            for name, since in list(self._stream_down_since.items()):
                if (
                    (now - since).total_seconds() >= self.settings.ws_alert_seconds
                    and name not in self._stream_warned
                ):
                    await self.telegram.send(
                        f"⚠️ BTC monitor warning: {name} stream unavailable for over 60 seconds."
                    )
                    self._stream_warned.add(name)
            if self.status == ServiceStatus.WARMING_UP:
                counts = await self.repository.flow_counts(self.settings.symbol)
                if self._warmup_complete(counts):
                    self.status = ServiceStatus.RUNNING
            elif self._stream_down_since:
                self.status = ServiceStatus.DEGRADED
            else:
                self.status = ServiceStatus.RUNNING

    def health(self, database_ok: bool) -> dict:
        streams = {
            name: {
                "connected": stream.connected,
                "last_message_at": stream.last_message_at,
                "reconnect_count": stream.reconnect_count,
            }
            for name, stream in self.streams.items()
        }
        healthy = database_ok and all(item["connected"] for item in streams.values())
        return {
            "status": self.status.value,
            "healthy": healthy,
            "strategy_version": "v1",
            "started_at": self.started_at,
            "last_finalized_candle_at": self.last_candle_at,
            "last_signal_at": self.last_signal_at,
            "last_error": self.last_error,
            "streams": streams,
        }

    def _warmup_complete(self, counts: dict[str, int]) -> bool:
        # The configured warmup is measured in 5-minute bars, while flow storage is 1-minute.
        required = self.settings.cvd_warmup_bars * 5
        return (
            counts.get(Market.SPOT.value, 0) >= required
            and counts.get(Market.LINEAR.value, 0) >= required
        )


def atr_or(value: Decimal | None, candles: list[Candle], fallback: Decimal) -> Decimal:
    return value or wilder_atr(candles)[-1] or fallback
