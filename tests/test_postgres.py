"""Opt-in PostgreSQL integration test; all test writes roll back.

RUN_POSTGRES_TESTS=1 python -m pytest tests/test_postgres.py
Uses DATABASE_URL from project settings. Never runs implicitly against a database.
"""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from btc_monitor.config import Settings
from btc_monitor.db import Database
from btc_monitor.domain import Candle, Direction, Divergence, FlowBar, Market, Pivot, Sweep
from btc_monitor.models import SignalRecord
from btc_monitor.repository import Repository
from btc_monitor.strategy import SignalEngine, StrategyContext
from btc_monitor.tracking import OutcomeTracker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1", reason="PostgreSQL integration is explicitly opt-in"
)
D = Decimal


async def test_hourly_feature_immutability_and_restart_persistence():
    from uuid import uuid4

    from btc_monitor.hourly.repository import HourlyRepository

    database = Database(Settings().database_url)
    try:
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            database.sessions = async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            repo = HourlyRepository(database)
            start = datetime(2020, 1, 1, tzinfo=UTC)
            bar = {
                "source": "coinalyze",
                "start": start.isoformat(),
                "decision_at": (start + timedelta(hours=1, minutes=3)).isoformat(),
                "complete": True,
            }
            await repo.save_bar(bar)
            await repo.save_bar({**bar, "complete": False})
            recovered = HourlyRepository(database)
            assert (await recovered.bars("coinalyze", start, start + timedelta(hours=1)))[0][
                "complete"
            ]
            assert await recovered.evaluated("coinalyze", start) is False
            await recovered.mark_evaluated("coinalyze", start)
            assert await repo.evaluated("coinalyze", start) is True
            alert_id = uuid4()
            values = {
                "id": alert_id,
                "source": "coinalyze",
                "bar_start": start,
                "generated_at": start,
                "direction": "SHORT",
                "tier": "WATCH",
                "origin": "retrospective",
                "entry": D(100),
                "invalidation": D(110),
                "snapshot": {"input_ids": [1]},
                "delivery_state": "retrospective",
                "tracking_complete": False,
            }
            assert await repo.save_alert(values) == alert_id
            assert await repo.save_alert({**values, "id": uuid4()}) is None
            result = {"windows": {"24": {"verdict": "insufficient_data"}}}
            await repo.save_outcomes(alert_id, result, True)
            row = await recovered.alert(alert_id)
            assert row.outcomes == result and row.tracking_complete
            assert row.snapshot == {"input_ids": [1]}
            await transaction.rollback()
    finally:
        await database.close()


async def test_snapshot_and_tracking_survive_repository_recreation():
    settings = Settings()
    database = Database(settings.database_url)
    try:
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            database.sessions = async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            repo = Repository(database)
            start = datetime(2026, 1, 1, 12, tzinfo=UTC)
            candle = Candle(start, D(100), D(102), D(98), D(101), D(1), D(200))
            pivot = Pivot(start - timedelta(hours=1), D(100), 0, "low")
            context = StrategyContext(
                "TESTBTC",
                Sweep(Direction.LONG, pivot, candle, D(2)),
                D(2),
                Divergence(True, D(2)),
                Divergence(True, D(2)),
                D(10),
                D(20),
                D(-1),
                None,
                None,
                None,
                [D(100)] * 20,
                None,
                None,
                None,
                [],
            )
            signal = SignalEngine(settings).evaluate(context)
            signal.generated_at = start + timedelta(minutes=5, seconds=1)
            signal.debug_snapshot["snapshot_origin"] = "integration_test"
            await repo.save_flow(
                FlowBar(Market.SPOT, "TESTBTC", start, D(100), D(50), D(50), D(50), 2, True)
            )
            assert (await repo.latest_flow(Market.SPOT, "TESTBTC")).is_complete
            assert await repo.save_signal(signal)
            assert not await repo.save_signal(signal)
            # Recreate the repository to prove tracking doesn't rely on in-memory signals.
            restored = Repository(database)
            loaded = await restored.get_signal(signal.signal_id)
            assert loaded.created_at == signal.generated_at
            assert loaded.debug_snapshot == signal.debug_snapshot
            await OutcomeTracker(restored, settings).update(start + timedelta(hours=3))
            tracked = await restored.get_signal(signal.signal_id)
            assert tracked.tracking_complete
            assert tracked.outcomes["windows"]["120"]["coverage"] == "incomplete"
            await transaction.rollback()
        database.sessions = async_sessionmaker(database.engine, expire_on_commit=False)
        async with database.session() as session:
            assert (
                await session.scalar(
                    select(SignalRecord).where(SignalRecord.id == signal.signal_id)
                )
                is None
            )
    finally:
        await database.close()


async def test_archive_live_writer_lock_is_exclusive_and_releasable():
    from btc_monitor.archive.store import ArchiveStore

    database = Database(Settings().database_url)
    store = ArchiveStore(database)
    first = second = None
    try:
        first = await store.acquire_writer()
        assert first is not None, "Stop the live local archive collector before running this test"
        assert await store.acquire_writer() is None
        await store.release_writer(first)
        first = None
        second = await store.acquire_writer()
        assert second is not None
    finally:
        if first is not None:
            await store.release_writer(first)
        if second is not None:
            await store.release_writer(second)
        await database.close()


async def test_archive_is_append_only_idempotent_and_replayable_after_restart():
    from btc_monitor.archive.exchanges import instruments
    from btc_monitor.archive.replay import window_features
    from btc_monitor.archive.store import ArchiveStore
    from btc_monitor.archive.types import Observation

    database = Database(Settings().database_url)
    try:
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            database.sessions = async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            store = ArchiveStore(database)
            start = datetime(2026, 1, 1, tzinfo=UTC)
            rows = [
                Observation(
                    inst.venue,
                    inst.market,
                    "INTEGRATION_TEST_BTC",
                    "flow",
                    60,
                    start,
                    start + timedelta(minutes=1),
                    datetime.now(UTC),
                    "rest_history",
                    "integration",
                    "complete",
                    {
                        "buy_base": "3",
                        "sell_base": "2",
                        "delta_base": "1",
                        "open": "100",
                        "high": "110",
                        "low": "90",
                        "close": "105",
                    },
                )
                for inst in instruments(("bybit",))
            ]
            assert await store.append(rows) == 2
            assert await store.append(rows) == 0
            restored = ArchiveStore(database)
            loaded = [
                row
                async for row in restored.scan(start, start + timedelta(minutes=1))
                if row["instrument"] == "INTEGRATION_TEST_BTC"
            ]
            assert len(loaded) == 2
            expected = [
                replace(inst, symbol="INTEGRATION_TEST_BTC") for inst in instruments(("bybit",))
            ]
            report = window_features(
                loaded,
                expected,
                start,
                start + timedelta(minutes=1),
                start + timedelta(minutes=1),
                "historical",
            )
            assert report["flow_complete"]
            causal = window_features(
                loaded, expected, start, start + timedelta(minutes=1), start + timedelta(minutes=1)
            )
            assert causal["flow_complete"] is False
            await transaction.rollback()
    finally:
        await database.close()
