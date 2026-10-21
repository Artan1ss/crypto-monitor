"""Postgres-backed independent evidence/state/outbox. No historical evidence updates."""
from contextlib import asynccontextmanager

from sqlalchemy import select, text, update, func
from sqlalchemy.dialects.postgresql import insert
from btc_monitor.models import SqueezeAlertRecord, SqueezeRuntimeRecord, MarketObservation


class SqueezeRepository:
    def __init__(self, database):
        self.db = database

    @asynccontextmanager
    async def lease(self):
        connection = await self.db.engine.connect()
        acquired = False
        try:
            try:
                acquired = bool(await connection.scalar(text("SELECT pg_try_advisory_lock(724196309)")))
                await connection.commit()
            except BaseException:
                await connection.invalidate()
                raise
            yield connection if acquired else None
        finally:
            try:
                if acquired:
                    await connection.execute(text("SELECT pg_advisory_unlock(724196309)"))
                    await connection.commit()
            except BaseException:
                await connection.invalidate()
                raise
            finally:
                await connection.close()

    async def state(self, version):
        async with self.db.session() as session:
            return await session.scalar(select(SqueezeRuntimeRecord.state).where(SqueezeRuntimeRecord.version==version))

    async def save(self, version, state, events):
        # Crash before commit advances neither cursor nor outbox; crash after
        # commit resumes both. A duplicate event can never overwrite evidence.
        async with self.db.session() as session:
            for event in events:
                await session.execute(insert(SqueezeAlertRecord).values(**event).on_conflict_do_nothing(index_elements=["event_key"]))
            await session.execute(insert(SqueezeRuntimeRecord).values(version=version,state=state)
                .on_conflict_do_update(index_elements=["version"],set_={"state":state}))

    async def pending(self):
        async with self.db.session() as session:
            return list((await session.scalars(select(SqueezeAlertRecord)
                .where(SqueezeAlertRecord.delivery_state=="pending",SqueezeAlertRecord.origin=="live")
                .order_by(SqueezeAlertRecord.generated_at).limit(50))).all())

    async def delivery(self, identity, state, at=None, message_id=None, expected="pending"):
        async with self.db.session() as session:
            result=await session.execute(update(SqueezeAlertRecord)
                .where(SqueezeAlertRecord.id==identity,SqueezeAlertRecord.delivery_state==expected)
                .values(delivery_state=state,delivery_at=at,delivery_id=message_id))
            return bool(result.rowcount)

    async def parent_sent(self, key):
        async with self.db.session() as session:
            state=await session.scalar(select(SqueezeAlertRecord.delivery_state).where(SqueezeAlertRecord.event_key==key))
            return state=="sent"

    async def alerts(self, limit=100):
        async with self.db.session() as session:
            return list((await session.scalars(select(SqueezeAlertRecord).order_by(SqueezeAlertRecord.generated_at.desc()).limit(limit))).all())

    async def alert(self, identity):
        async with self.db.session() as session:
            return await session.get(SqueezeAlertRecord,identity)

    async def counts(self):
        async with self.db.session() as session:
            return dict((await session.execute(select(SqueezeAlertRecord.delivery_state,func.count())
                .group_by(SqueezeAlertRecord.delivery_state))).all())

    async def raw(self,fingerprints):
        if len(fingerprints)>1000:
            raise ValueError("Raw export bound exceeded")
        async with self.db.session() as session:
            rows=list((await session.scalars(select(MarketObservation).where(
                MarketObservation.fingerprint.in_(fingerprints)))).all())
        return [{c.name:getattr(r,c.name) for c in r.__table__.columns} for r in rows]
