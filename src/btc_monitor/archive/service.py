from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog
import websockets

from btc_monitor.archive.coinalyze import CoinalyzeClient, CoinalyzeError, CoinalyzeSource
from btc_monitor.archive.exchanges import PublicRest, instruments, parse_message, subscription
from btc_monitor.archive.flow import MinuteCapture
from btc_monitor.archive.store import ArchiveStore
from btc_monitor.archive.types import Observation, minute
from btc_monitor.collectors.bybit import reconnect_delay

log = structlog.get_logger()


class ResearchCollector:
    """Seven independent public feeds; no strategy calls or Telegram messages."""

    def __init__(self, settings, store: ArchiveStore):
        self.settings, self.store = settings, store
        self.session = str(uuid4())
        self.rest = PublicRest(settings)
        self.captures = {
            i.key: MinuteCapture(i, self.session, datetime.now(UTC), settings.archive_grace_seconds)
            for i in instruments(settings.archive_venues)
        }
        self.queue: asyncio.Queue[Observation] = asyncio.Queue(settings.archive_queue_size)
        self.tasks: list[asyncio.Task] = []
        self.states = {
            key: {"connected": False, "reconnects": 0, "last_trade_at": None, "last_error": None}
            for key in self.captures
        }
        self.writer_lock = None
        self.last_saved_at: datetime | None = None
        self.last_storage_error: str | None = None
        self.waiting_for_writer = False
        self.saved_rows = 0
        self.started = False
        self._uncommitted: list[Observation] = []
        self._finalizing: list[Observation] = []
        self._polling: dict[str, list[Observation]] = {}
        self.coinalyze_client = None
        self.coinalyze_state = {
            "enabled": settings.coinalyze_enabled,
            "last_success_at": None,
            "last_error": None,
            "selected_feeds": [],
            "missing_feeds": [],
            "calibration": {},
            "series": [],
        }

    async def start(self):
        if self.settings.archive_enabled and not self.started:
            self.started = True
            self.tasks.append(asyncio.create_task(self._supervise(), name="archive-supervisor"))

    async def _supervise(self):
        # Rolling deploy may still have an old live writer. Wait without crashing V1.
        while self.writer_lock is None:
            try:
                self.writer_lock = await self.store.acquire_writer()
                self.waiting_for_writer = self.writer_lock is None
                self.last_storage_error = None
            except Exception as exc:
                self.last_storage_error = type(exc).__name__
            if self.writer_lock is None:
                await asyncio.sleep(5)
        now = datetime.now(UTC)
        for capture in self.captures.values():
            capture.cursor = minute(now)
        workers = [
            asyncio.create_task(self._persist(), name="archive-persistence"),
            asyncio.create_task(self._finalize(), name="archive-finalization"),
        ]
        for capture in self.captures.values():
            workers.append(asyncio.create_task(self._stream(capture), name=capture.instrument.key))
            if capture.instrument.market == "linear":
                workers.append(
                    asyncio.create_task(
                        self._poll(capture), name=f"archive-derivatives:{capture.instrument.venue}"
                    )
                )
        self.tasks.extend(workers)
        if self.settings.coinalyze_enabled:
            worker = asyncio.create_task(self._coinalyze(), name="archive-coinalyze")
            workers.append(worker)
            self.tasks.append(worker)
        try:
            # The session lock must remain held. A lost lock connection stops all feeds.
            while True:
                await asyncio.sleep(5)
                if any(task.done() for task in workers):
                    raise RuntimeError("Research worker exited unexpectedly")
                await self.writer_lock.exec_driver_sql("SELECT 1")
                await self.writer_lock.commit()
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for capture in self.captures.values():
                capture.connection(False, datetime.now(UTC))

    async def _stream(self, capture: MinuteCapture):
        inst, attempt = capture.instrument, 0
        state = self.states[inst.key]
        while True:
            try:
                # Contract metadata cannot be assumed. Refuse swaps when it is unavailable.
                inst, _ = await self.rest.metadata(inst)
                capture.instrument = inst
                url, request = subscription(inst, self.settings)
                async with websockets.connect(
                    url, open_timeout=15, close_timeout=3, ping_interval=20, ping_timeout=20
                ) as ws:
                    if request:
                        await ws.send(json.dumps(request))
                    # Binance raw streams need no subscription ACK.
                    acknowledged = request is None
                    if acknowledged:
                        capture.connection(True, datetime.now(UTC))
                        state["connected"] = True
                    last_data = datetime.now(UTC)
                    last_ping = datetime.now(UTC)
                    while True:
                        if inst.venue == "bybit" and datetime.now(UTC) - last_ping >= timedelta(
                            seconds=20
                        ):
                            await ws.send(json.dumps({"op": "ping"}))
                            last_ping = datetime.now(UTC)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        except TimeoutError:
                            if inst.venue == "okx":
                                await ws.send("ping")
                            if datetime.now(UTC) - last_data > timedelta(seconds=30):
                                raise TimeoutError("No BTC trade evidence") from None
                            continue
                        if raw == "pong":
                            continue
                        message = json.loads(raw)
                        if (
                            message.get("event") == "error"
                            or message.get("type") == "error"
                            or message.get("success") is False
                        ):
                            raise ValueError("Subscription rejected")
                        ack = (
                            message.get("op") == "subscribe"
                            and message.get("success") is True
                            or message.get("event") == "subscribe"
                            or message.get("type") == "subscriptions"
                        )
                        if ack and not acknowledged:
                            acknowledged = True
                            capture.connection(True, datetime.now(UTC))
                            state["connected"] = True
                        received = datetime.now(UTC)
                        trades = parse_message(inst, message, received)
                        if trades:
                            if not acknowledged:
                                raise ValueError("Trade received before subscription ACK")
                            for trade in trades:
                                capture.add(trade)
                            last_data = received
                            state["last_trade_at"] = received
                            state["last_error"] = None
                            attempt = 0
                        # Coinbase matches permits drops; heartbeat is independent evidence.
                        if inst.venue == "coinbase" and message.get("type") == "heartbeat":
                            last_id = int(message["last_trade_id"])
                            if capture.sequence is not None and last_id > capture.sequence:
                                capture.invalidate(
                                    minute(received), "heartbeat_missing_trade", received
                                )
                        if received - last_data > timedelta(seconds=30):
                            raise TimeoutError("No BTC trade evidence")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                capture.connection(False, datetime.now(UTC))
                state["connected"] = False
                state["last_error"] = type(exc).__name__
                state["reconnects"] += 1
                log.warning("archive_feed_retry", feed=inst.key, error_type=type(exc).__name__)
                await asyncio.sleep(reconnect_delay(attempt))
                attempt = min(attempt + 1, 5)

    async def _poll(self, capture: MinuteCapture):
        while True:
            try:
                rows = await self.rest.derivatives(capture.instrument, self.session)
                self._polling[capture.instrument.key] = rows
                while rows:
                    await self.queue.put(rows[0])
                    rows.pop(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.states[capture.instrument.key]["derivatives_error"] = type(exc).__name__
                log.warning(
                    "archive_derivatives_retry",
                    venue=capture.instrument.venue,
                    error_type=type(exc).__name__,
                )
            else:
                self.states[capture.instrument.key]["derivatives_error"] = None
            await asyncio.sleep(self.settings.archive_poll_seconds)

    async def _coinalyze(self):
        self.coinalyze_client = CoinalyzeClient(self.settings)
        source = CoinalyzeSource(self.settings, self.coinalyze_client)
        state = self.coinalyze_state
        prepared = False
        while True:
            delay = self.settings.coinalyze_poll_seconds
            try:
                if (
                    not prepared
                    or source.discovery_at is None
                    or datetime.now(UTC) - source.discovery_at > timedelta(days=1)
                ):
                    discovered = await source.discover()
                    await source.calibrate(self.rest)
                    discovered = source.catalog()
                    state["selected_feeds"] = discovered["selected"]
                    state["missing_feeds"] = source.missing
                    state["calibration"] = source.calibration
                    metadata = source.metadata_observation()
                    self._polling["coinalyze"] = [metadata]
                    await self.queue.put(metadata)
                    self._polling["coinalyze"] = []
                    prepared = True
                end = minute(datetime.now(UTC))
                start = end - timedelta(minutes=5)
                series = []
                for market in source.markets:
                    for endpoint in source.datasets(market):
                        rows = await source.observations(market, endpoint, start, end)
                        series.append(
                            {
                                "feed": market.instrument.key,
                                "endpoint": endpoint,
                                "returned": len(rows),
                                "expected_bars": 5,
                            }
                        )
                        self._polling["coinalyze"] = rows
                        while rows:
                            await self.queue.put(rows[0])
                            rows.pop(0)
                state["series"] = series
                state["last_success_at"] = datetime.now(UTC)
                state["last_error"] = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state["last_error"] = (
                    exc.reason if isinstance(exc, CoinalyzeError) else type(exc).__name__
                )
                if isinstance(exc, CoinalyzeError) and exc.status in {401, 403}:
                    delay = max(delay, 300)
                log.warning("archive_coinalyze_retry", error_type=state["last_error"])
            await asyncio.sleep(delay)

    async def _finalize(self):
        while True:
            now = datetime.now(UTC)
            for capture in self.captures.values():
                self._finalizing = capture.finalized(now)
                while self._finalizing:
                    await self.queue.put(self._finalizing[0])
                    self._finalizing.pop(0)
            await asyncio.sleep(1)

    async def _persist(self):
        batch: list[Observation] = []
        while True:
            if not batch:
                batch.append(await self.queue.get())
                while len(batch) < 200 and not self.queue.empty():
                    batch.append(self.queue.get_nowait())
            self._uncommitted = batch
            try:
                self.saved_rows += await self.store.append(batch)
            except asyncio.CancelledError:
                # Stop() gets an explicit chance to flush the held batch.
                self._uncommitted = batch
                raise
            except Exception as exc:
                self.last_storage_error = type(exc).__name__
                log.warning("archive_storage_retry", error_type=type(exc).__name__)
                await asyncio.sleep(5)
            else:
                self.last_storage_error = None
                self.last_saved_at = datetime.now(UTC)
                for _ in batch:
                    self.queue.task_done()
                batch = []
                self._uncommitted = []

    def health(self):
        enabled = self.settings.archive_enabled
        now = datetime.now(UTC)
        tasks_alive = {t.get_name(): not t.done() for t in self.tasks}
        provider = dict(self.coinalyze_state)
        provider["healthy"] = (
            provider["enabled"]
            and not provider["last_error"]
            and provider["last_success_at"] is not None
            and now - provider["last_success_at"]
            <= timedelta(seconds=self.settings.coinalyze_poll_seconds + 90)
        )
        provider["coverage_complete"] = (
            provider["healthy"]
            and not provider["missing_feeds"]
            and bool(provider["series"])
            and all(s["returned"] == s["expected_bars"] for s in provider["series"])
        )
        provider["request_count"] = (
            self.coinalyze_client.request_count if self.coinalyze_client else 0
        )
        provider["symbol_cost"] = self.coinalyze_client.symbol_cost if self.coinalyze_client else 0
        return {
            "enabled": enabled,
            "session_id": self.session if enabled else None,
            "healthy": enabled
            and bool(tasks_alive)
            and all(tasks_alive.values())
            and all(s["connected"] for s in self.states.values())
            and all(
                s["last_trade_at"] is not None and now - s["last_trade_at"] <= timedelta(seconds=30)
                for s in self.states.values()
            )
            and all(not s.get("derivatives_error") for s in self.states.values())
            and not self.last_storage_error
            and self.last_saved_at is not None
            and now - self.last_saved_at
            <= timedelta(seconds=max(90, self.settings.archive_poll_seconds + 30))
            and not self.waiting_for_writer
            and (not provider["enabled"] or provider["healthy"]),
            "coinalyze": provider,
            "waiting_for_writer": self.waiting_for_writer,
            "feeds": self.states,
            "tasks": tasks_alive,
            "pending_rows": self.queue.qsize()
            + len(self._uncommitted)
            + len(self._finalizing)
            + sum(len(rows) for rows in self._polling.values()),
            "saved_rows_this_session": self.saved_rows,
            "last_saved_at": self.last_saved_at,
            "storage_error": self.last_storage_error,
        }

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        remaining = list(self._uncommitted) + list(self._finalizing)
        for rows in self._polling.values():
            remaining.extend(rows)
        while not self.queue.empty():
            remaining.append(self.queue.get_nowait())
        now = datetime.now(UTC)
        for capture in self.captures.values():
            # Include current unfinished minute as partial, never as a complete bar.
            capture.connection(False, now)
            remaining.extend(capture.finalized(minute(now) + timedelta(minutes=1) + capture.grace))
        if remaining and self.writer_lock is not None:
            try:
                for offset in range(0, len(remaining), 200):
                    await asyncio.wait_for(self.store.append(remaining[offset : offset + 200]), 10)
            except Exception as exc:
                log.error(
                    "archive_shutdown_unflushed", error_type=type(exc).__name__, rows=len(remaining)
                )
        if self.writer_lock is not None:
            try:
                await self.store.release_writer(self.writer_lock)
            except Exception as exc:
                log.warning("archive_lock_release_failed", error_type=type(exc).__name__)
            self.writer_lock = None
        await self.rest.close()
        if self.coinalyze_client:
            await self.coinalyze_client.close()
