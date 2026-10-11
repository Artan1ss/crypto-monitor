"""Explicit opt-in public network checks; no account, Telegram or production DB writes."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from btc_monitor.archive.exchanges import PublicRest, instruments
from btc_monitor.archive.history import HistoryLoader
from btc_monitor.archive.service import ResearchCollector
from btc_monitor.archive.types import serializable
from btc_monitor.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_ARCHIVE_TESTS") != "1", reason="Live public APIs are explicitly opt-in"
)


class MemoryStore:
    def __init__(self):
        self.rows = []

    async def append(self, rows):
        self.rows.extend(rows)
        return len(rows)

    async def acquire_writer(self):
        from unittest.mock import AsyncMock

        return AsyncMock()

    async def release_writer(self, connection):
        pass


@pytest.mark.parametrize(
    "inst", instruments(("bybit", "binance", "okx", "coinbase")), ids=lambda i: i.key
)
async def test_public_closed_minute_backfill(inst):
    settings = Settings(_env_file=None)
    rest, store = PublicRest(settings), MemoryStore()
    end = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=10)
    start = end - timedelta(minutes=3)
    try:
        report = await HistoryLoader(rest, store).prices_and_flow(inst, start, end)
        assert report["returned"] == 3, (
            start,
            end,
            [(r.event_time, r.payload) for r in store.rows],
        )
        assert len(store.rows) == 3
        assert all(start <= r.event_time < r.end_time <= end for r in store.rows)
        assert all(r.received_at >= end for r in store.rows)
        assert {r.dataset for r in store.rows} == (
            {"flow"} if inst.venue == "binance" else {"candle"}
        )
    finally:
        await rest.close()


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_CAPTURE_TEST") != "1", reason="150-second live capture is separately opt-in"
)
async def test_seven_feeds_capture_complete_minutes_without_v1():
    import asyncio
    import json

    config = Settings(_env_file=None, archive_enabled=True)
    store = MemoryStore()
    collector = ResearchCollector(config, store)
    try:
        await collector.start()
        await asyncio.sleep(150)
        print(json.dumps(serializable(collector.health()), indent=2))
    finally:
        await collector.stop()
    complete = {
        f"{r.venue}:{r.market}:{r.instrument}"
        for r in store.rows
        if r.dataset == "flow" and r.quality == "complete"
    }
    assert complete == set(collector.captures), {
        key: [
            r.payload.get("quality_reasons")
            for r in store.rows
            if f"{r.venue}:{r.market}:{r.instrument}" == key and r.dataset == "flow"
        ]
        for key in collector.captures
        if key not in complete
    }
