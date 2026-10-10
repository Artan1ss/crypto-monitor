from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from btc_monitor.diagnostics import cvd_diagnostics, flow_row_snapshot, json_value
from btc_monitor.domain import ZERO, Candle, Direction, Market, Pivot, ServiceStatus
from btc_monitor.indicators import (
    confirmed_pivots,
    detect_sweep,
    ema,
    percent_change,
    wilder_atr,
)
from btc_monitor.repository import Repository
from btc_monitor.strategy import SignalEngine, StrategyContext
from btc_monitor.telegram import TelegramClient, render_signal
from btc_monitor.tracking import OutcomeTracker

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
        self._candle_queue: asyncio.Queue = asyncio.Queue()
        self.tracker = OutcomeTracker(repository, settings)
        self.last_tracking_error: str | None = None
        source = Path(__file__).parent
        self.code_fingerprint = hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(source.rglob("*.py")))
        ).hexdigest()

    async def start(self) -> None:
        await self.flow.initialize()
        await self._backfill()
        recent = await self.repository.recent_signals(1)
        if recent:
            self.last_signal_at = recent[0].signal_time
        warmup_counts = await self.repository.flow_counts(
            self.settings.symbol, self.settings.cvd_warmup_bars * 5
        )
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
            asyncio.create_task(self._evaluation_loop(), name="signal-evaluation"),
            asyncio.create_task(self._tracking_loop(), name="signal-outcomes"),
        ]
        if self.settings.send_startup_message:
            message = (
                "BTC monitor online — collecting initial CVD history."
                if self.status == ServiceStatus.WARMING_UP
                else "BTC monitor online — stored CVD history restored."
            )
            await self._safe_notification(message)
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
            if atr is not None:
                self._candle_queue.put_nowait((candle, atr))
        else:
            closes = [c.close for c in series]
            value50, value200 = ema(closes, 50)[-1], ema(closes, 200)[-1]
            await self.repository.save_candle(
                self.settings.symbol, candle, "60", ema50=value50, ema200=value200
            )

    async def _evaluation_loop(self) -> None:
        while True:
            candle, atr = await self._candle_queue.get()
            try:
                end = candle.timestamp + timedelta(minutes=5)
                # Separate worker: never block the Linear message consumer while waiting
                # for its next trade. Independent Spot/Linear arrival order is normal.
                for _ in range(50):
                    if all(
                        self.flow.watermarks.get(m, datetime.min.replace(tzinfo=UTC)) >= end
                        for m in Market
                    ):
                        break
                    await asyncio.sleep(0.1)
                else:
                    log.warning(
                        "signal_evaluation_skipped",
                        reason="flow_watermark_timeout",
                        candle=candle.timestamp.isoformat(),
                    )
                    continue
                if self.candles_5m[-1].timestamp != candle.timestamp:
                    log.warning("signal_evaluation_skipped", reason="stale_queue_item")
                    continue
                await self._evaluate(atr)
            except Exception as exc:
                self.last_error = f"evaluation: {exc}"
                log.exception("signal_evaluation_failed")
            finally:
                self._candle_queue.task_done()

    async def _tracking_loop(self) -> None:
        while True:
            try:
                await self.tracker.update(datetime.now(UTC))
                self.last_tracking_error = None
            except Exception as exc:
                self.last_tracking_error = str(exc)
                log.exception("signal_tracking_failed")
            await asyncio.sleep(10)

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
            if not all(d["data_valid"] for d in self._context_snapshot["cvd"].values()):
                log.warning(
                    "signal_evaluation_skipped",
                    reason="invalid_cvd_data",
                    diagnostics=self._context_snapshot["cvd"],
                )
                continue
            signal = self.engine.evaluate(context)
            if signal and self._cooldown_ready(signal.timestamp):
                signal.generated_at = datetime.now(UTC)
                signal.debug_snapshot.update(self._context_snapshot)
                signal.debug_snapshot.update(
                    json_value(
                        {
                            "schema_version": 1,
                            "implementation_version": "v1-event-time-diagnostics-1",
                            "source_sha256": self.code_fingerprint,
                            "snapshot_origin": "live_at_decision",
                            "generated_at": signal.generated_at,
                            "candle_start": current.timestamp,
                            "candle_close": current.timestamp + timedelta(minutes=5),
                            "decision_latency_seconds": (
                                signal.generated_at - current.timestamp - timedelta(minutes=5)
                            ).total_seconds(),
                            "parameters": self.settings.model_dump(
                                exclude={
                                    "database_url",
                                    "telegram_bot_token",
                                    "telegram_chat_id",
                                }
                            ),
                            "streams": self.health(True)["streams"],
                            "flow_watermarks": self.flow.watermarks,
                            "flow_ingestion_stats": self.flow.ingestion_stats,
                            "signal_features": signal.feature_payload(),
                            "decision": {
                                "signal_id": signal.signal_id,
                                "direction": signal.direction,
                                "score": signal.score,
                                "strength": signal.strength,
                                "entry_reference": signal.entry_reference,
                                "stop_reference": signal.stop_reference,
                                "tp1": signal.tp1,
                                "tp2": signal.tp2,
                                "tp3": signal.tp3,
                                "structure_target": signal.structure_target,
                                "reasons": signal.reasons,
                                "warnings": signal.warnings,
                            },
                        }
                    )
                )
                inserted = await self.repository.save_signal(signal)
                if not inserted:
                    self.consumed.add(key)
                    continue
                self.consumed.add(key)
                self.last_signal_at = signal.timestamp
                try:
                    message_id = await self.telegram.send(render_signal(signal))
                    if message_id:
                        await self.repository.mark_telegram_sent(signal.signal_id, message_id)
                except Exception as exc:
                    self.last_error = f"telegram: {type(exc).__name__}"
                    log.error(
                        "telegram_delivery_failed",
                        signal_id=str(signal.signal_id),
                        error_type=type(exc).__name__,
                    )
                log.info(
                    "signal_created",
                    id=str(signal.signal_id),
                    score=str(signal.score),
                    direction=signal.direction.value,
                )

    async def _strategy_context(self, sweep, opposing: list[Pivot]) -> StrategyContext:
        end = sweep.candle.timestamp + timedelta(minutes=5)
        five_minute_inputs = list(self.candles_5m)
        spot_rows, linear_rows = await asyncio.gather(
            self.repository.load_flow_rows(self.settings.symbol, Market.SPOT, end=end),
            self.repository.load_flow_rows(self.settings.symbol, Market.LINEAR, end=end),
        )
        spot_divergence, spot_delta, spot_debug = cvd_diagnostics(
            spot_rows,
            sweep.pivot.timestamp,
            sweep.candle.timestamp,
            sweep.direction,
            min(20, self.settings.cvd_warmup_bars),
        )
        linear_divergence, linear_delta, linear_debug = cvd_diagnostics(
            linear_rows,
            sweep.pivot.timestamp,
            sweep.candle.timestamp,
            sweep.direction,
            min(20, self.settings.cvd_warmup_bars),
        )
        oi_now, oi5_row, oi15_row = await asyncio.gather(
            self.repository.ticker_at(self.settings.symbol, timedelta(0), as_of=end),
            self.repository.ticker_at(self.settings.symbol, timedelta(minutes=5), as_of=end),
            self.repository.ticker_at(self.settings.symbol, timedelta(minutes=15), as_of=end),
        )
        oi_current = oi_now.open_interest if oi_now else None
        raw_ticker_rows = (oi_now, oi5_row, oi15_row)

        def fresh(row, target):
            return row is not None and (target - row.timestamp).total_seconds() <= 15

        oi_current = oi_current if fresh(oi_now, end) else None
        oi5_row = oi5_row if fresh(oi5_row, end - timedelta(minutes=5)) else None
        oi15_row = oi15_row if fresh(oi15_row, end - timedelta(minutes=15)) else None
        oi5 = percent_change(oi_current, oi5_row.open_interest if oi5_row else None)
        oi15 = percent_change(oi_current, oi15_row.open_interest if oi15_row else None)
        previous = five_minute_inputs[-2].close
        price_change = percent_change(five_minute_inputs[-1].close, previous) or ZERO
        htf = [c for c in self.candles_1h if c.timestamp + timedelta(hours=1) <= end]
        htf_close = htf[-1].close if htf else None
        closes = [c.close for c in htf]
        ema50_value = ema(closes, 50)[-1] if len(closes) >= 50 else None
        ema200_value = ema(closes, 200)[-1] if len(closes) >= 200 else None
        metrics = self.orderbook.metrics()
        active, structure_debug = self._active_structure(opposing)
        ticker_debug = {
            name: json_value(
                {
                    key: getattr(row, key, None)
                    for key in ("timestamp", "open_interest", "funding_rate", "last_price")
                }
            )
            if row
            else None
            for name, row in (
                ("at_close", raw_ticker_rows[0]),
                ("five_minutes_before", raw_ticker_rows[1]),
                ("fifteen_minutes_before", raw_ticker_rows[2]),
            )
        }
        self._context_snapshot = json_value(
            {
                "sweep": sweep,
                "candles_5m": five_minute_inputs,
                "candles_1h": htf,
                "cvd": {"spot": spot_debug, "linear": linear_debug},
                "raw_flow_rows": {
                    "spot": [flow_row_snapshot(r) for r in spot_rows],
                    "linear": [flow_row_snapshot(r) for r in linear_rows],
                },
                "ticker_inputs": ticker_debug,
                "ticker_cutoff": end,
                "ticker_freshness_limit_seconds": 15,
                "htf_inputs": {
                    "candle": htf[-1] if htf else None,
                    "ema50": ema50_value,
                    "ema200": ema200_value,
                },
                "structure_candidates": structure_debug,
                "orderbook_metrics": metrics,
            }
        )
        return StrategyContext(
            symbol=self.settings.symbol,
            sweep=sweep,
            atr=atr_or(sweep.candle.atr14, five_minute_inputs, Decimal("1")),
            spot_divergence=spot_divergence,
            futures_divergence=linear_divergence,
            spot_delta_5m=spot_delta,
            futures_delta_5m=linear_delta,
            price_change_5m=price_change,
            oi_change_5m=oi5,
            oi_change_15m=oi15,
            funding_rate=oi_now.funding_rate if fresh(oi_now, end) else None,
            previous_turnovers=[c.turnover for c in five_minute_inputs[-21:-1]],
            htf_close=htf_close,
            htf_ema50=ema50_value,
            htf_ema200=ema200_value,
            opposing_pivots=active,
            orderbook_imbalance=metrics.get("imbalance_025") if metrics else None,
        )

    def _active_structure(self, pivots):
        active, debug = [], []
        for pivot in pivots:
            breached = next(
                (
                    c
                    for c in self.candles_5m[pivot.index + 1 :]
                    if (c.high >= pivot.price if pivot.kind == "high" else c.low <= pivot.price)
                ),
                None,
            )
            if breached is None:
                active.append(pivot)
            debug.append(
                {
                    "pivot": pivot,
                    "active": breached is None,
                    "first_breach_at": breached.timestamp if breached else None,
                }
            )
        return active, debug

    def _cooldown_ready(self, timestamp: datetime) -> bool:
        return self.last_signal_at is None or timestamp - self.last_signal_at >= timedelta(
            minutes=self.settings.signal_cooldown_min
        )

    async def _ticker_persist_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.ticker_persist_seconds)
            if self.ticker.get("last_price") is not None:
                try:
                    await self.repository.save_ticker(self.settings.symbol, dict(self.ticker))
                except Exception as exc:
                    self.last_error = f"ticker_persistence: {type(exc).__name__}"
                    log.exception("ticker_persistence_failed")

    async def _safe_notification(self, message: str) -> None:
        try:
            await self.telegram.send(message)
        except Exception as exc:
            self.last_error = f"telegram_notification: {type(exc).__name__}"
            log.error("telegram_notification_failed", error_type=type(exc).__name__)

    async def _stream_state(self, name: str, connected: bool) -> None:
        if connected:
            if name in self._stream_warned:
                await self._safe_notification(
                    f"✅ BTC monitor recovered: {name} stream reconnected."
                )
            self._stream_down_since.pop(name, None)
            self._stream_warned.discard(name)
        else:
            await self.flow.invalidate(Market(name))
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
                    await self._safe_notification(
                        f"⚠️ BTC monitor warning: {name} stream unavailable for over 60 seconds."
                    )
                    self._stream_warned.add(name)
            try:
                counts = await self.repository.flow_counts(
                    self.settings.symbol, self.settings.cvd_warmup_bars * 5
                )
            except Exception as exc:
                self.status = ServiceStatus.DEGRADED
                self.last_error = f"warmup_health: {type(exc).__name__}"
                log.exception("runtime_health_query_failed")
                continue
            if self._stream_down_since:
                self.status = ServiceStatus.DEGRADED
            elif not self._warmup_complete(counts):
                self.status = ServiceStatus.WARMING_UP
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
        background_tasks = {task.get_name(): not task.done() for task in self.tasks}
        healthy = (
            database_ok
            and bool(streams)
            and all(item["connected"] for item in streams.values())
            and all(background_tasks.values())
        )
        return {
            "status": self.status.value,
            "healthy": healthy,
            "strategy_version": "v1",
            "started_at": self.started_at,
            "last_finalized_candle_at": self.last_candle_at,
            "last_signal_at": self.last_signal_at,
            "last_error": self.last_error,
            "last_tracking_error": self.last_tracking_error,
            "streams": streams,
            "background_tasks": background_tasks,
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
