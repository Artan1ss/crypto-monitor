from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from btc_monitor.models import HourlyAlertRecord, HourlyBarRecord, MarketObservation, TickerSnapshot


class HourlyRepository:
    def __init__(self, database):
        self.db = database

    @asynccontextmanager
    async def lease(self):
        connection = await self.db.engine.connect()
        acquired = False
        try:
            try:
                acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(724196307)"))
                await connection.commit()
            except BaseException:
                # Cancellation can arrive after PostgreSQL acquired the session lock.
                await connection.invalidate()
                raise
            yield connection if acquired else None
        finally:
            try:
                if acquired:
                    await connection.execute(text("SELECT pg_advisory_unlock(724196307)"))
                    await connection.commit()
            except BaseException:
                await connection.invalidate()
                raise
            finally:
                await connection.close()

    async def observations(self, start, end, source=None, price_only=False):
        query = select(MarketObservation).where(
            MarketObservation.end_time >= start, MarketObservation.event_time < end
        )
        if source == "coinalyze":
            query = query.where(MarketObservation.source_type == "coinalyze_history")
        elif source == "direct":
            query = query.where(MarketObservation.source_type == "live")
        if price_only:
            query = query.where(
                MarketObservation.venue == "bybit",
                MarketObservation.market == "linear",
                MarketObservation.instrument == "BTCUSDT",
                MarketObservation.dataset.in_(["flow", "candle", "quality"]),
            )
        # One-hour feature inputs or 24H outcome paths only, never unbounded tick history.
        query = query.order_by(MarketObservation.id).limit(50001)
        async with self.db.session() as session:
            rows = list((await session.scalars(query)).all())
        if len(rows) > 50000:
            raise ValueError("Hourly observation range exceeds safe row bound")
        return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]

    async def save_bar(self, bar):
        async with self.db.session() as session:
            await session.execute(
                insert(HourlyBarRecord)
                .values(
                    source=bar["source"],
                    start=datetime.fromisoformat(bar["start"]),
                    decision_at=datetime.fromisoformat(bar["decision_at"]),
                    features=bar,
                )
                .on_conflict_do_nothing(index_elements=["source", "start"])
            )

    async def evaluated(self, source, start):
        async with self.db.session() as session:
            return await session.scalar(
                select(HourlyBarRecord.evaluated).where(
                    HourlyBarRecord.source == source, HourlyBarRecord.start == start
                )
            )

    async def mark_evaluated(self, source, start):
        async with self.db.session() as session:
            await session.execute(
                update(HourlyBarRecord)
                .where(HourlyBarRecord.source == source, HourlyBarRecord.start == start)
                .values(evaluated=True)
            )

    async def bars(self, source, start, end):
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(HourlyBarRecord)
                    .where(
                        HourlyBarRecord.source == source,
                        HourlyBarRecord.start >= start,
                        HourlyBarRecord.start < end,
                    )
                    .order_by(HourlyBarRecord.start)
                )
            ).all()
        return [r.features for r in rows]

    async def save_alert(self, values):
        async with self.db.session() as session:
            return await session.scalar(
                insert(HourlyAlertRecord)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["source", "bar_start", "direction", "tier"])
                .returning(HourlyAlertRecord.id)
            )

    async def cooling_down(self, source, bar_start, direction, tier, hours):
        async with self.db.session() as session:
            return (
                await session.scalar(
                    select(HourlyAlertRecord.id)
                    .where(
                        HourlyAlertRecord.source == source,
                        HourlyAlertRecord.bar_start < bar_start,
                        HourlyAlertRecord.bar_start >= bar_start - timedelta(hours=hours),
                        HourlyAlertRecord.direction == direction,
                        HourlyAlertRecord.tier == tier,
                        HourlyAlertRecord.origin == "live",
                        HourlyAlertRecord.delivery_state.in_(
                            ["pending", "attempted", "sent", "uncertain"]
                        ),
                    )
                    .limit(1)
                )
                is not None
            )

    async def alerts(self, source, limit=200, start=None, end=None, pending=False):
        query = select(HourlyAlertRecord).where(HourlyAlertRecord.source == source)
        if start:
            query = query.where(HourlyAlertRecord.bar_start >= start)
        if end:
            query = query.where(HourlyAlertRecord.bar_start < end)
        if pending:
            query = query.where(HourlyAlertRecord.tracking_complete.is_(False))
        async with self.db.session() as session:
            return list(
                (
                    await session.scalars(
                        query.order_by(HourlyAlertRecord.bar_start.desc()).limit(limit)
                    )
                ).all()
            )

    async def alert(self, alert_id):
        async with self.db.session() as session:
            return await session.get(HourlyAlertRecord, alert_id)

    async def pending_deliveries(self):
        async with self.db.session() as session:
            return list(
                (
                    await session.scalars(
                        select(HourlyAlertRecord)
                        .where(
                            HourlyAlertRecord.delivery_state == "pending",
                            HourlyAlertRecord.origin == "live",
                        )
                        .order_by(HourlyAlertRecord.generated_at)
                        .limit(50)
                    )
                ).all()
            )

    async def set_delivery(self, alert_id, state, at=None, message_id=None, expected=None):
        query = update(HourlyAlertRecord).where(HourlyAlertRecord.id == alert_id)
        if expected:
            query = query.where(HourlyAlertRecord.delivery_state == expected)
        async with self.db.session() as session:
            result = await session.execute(
                query.values(delivery_state=state, delivery_at=at, delivery_id=message_id)
            )
            return bool(result.rowcount)

    async def save_outcomes(self, alert_id, result, complete):
        async with self.db.session() as session:
            await session.execute(
                update(HourlyAlertRecord)
                .where(HourlyAlertRecord.id == alert_id)
                .values(outcomes=result, tracking_complete=complete)
            )

    async def entry_sample(self, now):
        async with self.db.session() as session:
            row = await session.scalar(
                select(TickerSnapshot)
                .where(
                    TickerSnapshot.symbol == "BTCUSDT",
                    TickerSnapshot.timestamp <= now,
                    TickerSnapshot.timestamp >= now - timedelta(seconds=30),
                )
                .order_by(TickerSnapshot.timestamp.desc())
                .limit(1)
            )
            return (row.last_price, row.timestamp) if row else (None, None)

    async def raw_inputs(self, ids):
        if len(ids) > 25000:
            raise ValueError("Snapshot exceeds export bound")
        async with self.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(MarketObservation)
                        .where(MarketObservation.id.in_(ids))
                        .order_by(MarketObservation.id)
                    )
                ).all()
            )
        if len(rows) != len(set(ids)):
            raise ValueError("Immutable archive inputs are missing")
        return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]

    async def metadata(self, decision):
        async with self.db.session() as session:
            row = await session.scalar(
                select(MarketObservation)
                .where(
                    MarketObservation.dataset == "provider_metadata",
                    MarketObservation.available_at <= decision,
                )
                .order_by(MarketObservation.available_at.desc(), MarketObservation.id.desc())
                .limit(1)
            )
        return {c.name: getattr(row, c.name) for c in row.__table__.columns} if row else None


def alert_json(row, detail=False):
    value = {
        key: getattr(row, key)
        for key in (
            "id",
            "source",
            "bar_start",
            "generated_at",
            "direction",
            "tier",
            "origin",
            "entry",
            "invalidation",
            "outcomes",
            "tracking_complete",
            "delivery_state",
            "delivery_at",
        )
    }
    value["kind"] = row.snapshot["candidate"]["kind"]
    value["reasons"] = row.snapshot["candidate"]["reasons"]
    if detail:
        value["snapshot"] = row.snapshot
    return value
