from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from btc_monitor.archive.types import Observation
from btc_monitor.db import Database
from btc_monitor.models import MarketObservation


class ArchiveStore:
    def __init__(self, database: Database):
        self.database = database

    async def append(self, observations: list[Observation]) -> int:
        if not observations:
            return 0
        # One atomic batch. No overwrite: later revisions remain separately auditable.
        values = [o.values() for o in observations]
        async with self.database.session() as session:
            available = await session.scalar(select(func.clock_timestamp()))
            for row in values:
                row["available_at"] = max(available, row["received_at"])
            result = await session.execute(
                insert(MarketObservation)
                .values(values)
                .on_conflict_do_nothing(index_elements=[MarketObservation.fingerprint])
                .returning(MarketObservation.id)
            )
            return len(result.all())

    async def scan(
        self, start: datetime, end: datetime, available_by: datetime | None = None
    ) -> AsyncIterator[dict]:
        """Keyset pagination, no unbounded ORM load; include overlapping quality gaps."""
        cursor = 0
        while True:
            statement = (
                select(MarketObservation)
                .where(
                    MarketObservation.end_time >= start,
                    MarketObservation.event_time < end,
                    MarketObservation.id > cursor,
                )
                .order_by(MarketObservation.id)
                .limit(2000)
            )
            if available_by is not None:
                statement = statement.where(MarketObservation.available_at <= available_by)
            async with self.database.session() as session:
                rows = list((await session.scalars(statement)).all())
            if not rows:
                return
            for row in rows:
                cursor = row.id
                yield {c.name: getattr(row, c.name) for c in row.__table__.columns}

    async def summary(self) -> list[dict]:
        async with self.database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(
                            MarketObservation.venue,
                            MarketObservation.market,
                            MarketObservation.dataset,
                            MarketObservation.source_type,
                            func.count().label("rows"),
                            func.min(MarketObservation.event_time).label("first_event"),
                            func.max(MarketObservation.end_time).label("last_event"),
                            func.max(MarketObservation.available_at).label("last_saved"),
                        ).group_by(
                            MarketObservation.venue,
                            MarketObservation.market,
                            MarketObservation.dataset,
                            MarketObservation.source_type,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def acquire_writer(self):
        """Session lock prevents overlapping deploys from double-counting live streams."""
        connection = await self.database.engine.connect()
        try:
            acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(724196306)"))
            # This is a session lock: commit preserves ownership without leaving
            # an idle transaction open for the lifetime of the collector.
            await connection.commit()
        except BaseException:
            # Session locks survive rollback/pool return. If acquisition was cancelled,
            # discard the physical connection rather than possibly pool a held lock.
            await connection.invalidate()
            await connection.close()
            raise
        if not acquired:
            await connection.close()
            return None
        return connection

    async def release_writer(self, connection):
        try:
            await connection.execute(text("SELECT pg_advisory_unlock(724196306)"))
            await connection.commit()
        except BaseException:
            await connection.invalidate()
            raise
        finally:
            await connection.close()
