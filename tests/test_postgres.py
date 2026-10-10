"""Opt-in PostgreSQL integration test; all test writes roll back.

RUN_POSTGRES_TESTS=1 python -m pytest tests/test_postgres.py
Uses DATABASE_URL from project settings. Never runs implicitly against a database.
"""

import os
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
