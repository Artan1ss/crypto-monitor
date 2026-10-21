"""Explicit opt-in against an isolated temporary PostgreSQL, never production."""
import copy
import os
from datetime import datetime,timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from btc_monitor.db import Database
from btc_monitor.squeeze.repository import SqueezeRepository
from btc_monitor.squeeze.service import RUNTIME_VERSION,verify_writer
from test_squeeze_runtime import live_case

pytestmark=pytest.mark.skipif(os.getenv("RUN_SQUEEZE_POSTGRES")!="1",reason="isolated PostgreSQL check is opt-in")


async def test_atomic_restart_state_outbox_unique_evidence_and_compare_swap():
    database=Database(os.environ["DATABASE_URL"])
    try:
        async with database.engine.connect() as connection:
            transaction=await connection.begin()
            database.sessions=async_sessionmaker(bind=connection,expire_on_commit=False,join_transaction_mode="create_savepoint")
            data,state,event=live_case()
            repo=SqueezeRepository(database)
            await repo.save(RUNTIME_VERSION,state,[event])
            assert await repo.state(RUNTIME_VERSION)==state
            assert len(await repo.pending())==1
            changed=copy.deepcopy(event);changed["payload"]["price_reference"]="999999"
            await repo.save(RUNTIME_VERSION,state,[changed])
            row=await repo.alert(event["id"])
            assert row.payload==event["payload"]
            assert await repo.delivery(event["id"],"attempted",expected="pending")
            assert not await repo.delivery(event["id"],"attempted",expected="pending")
            assert await repo.delivery(event["id"],"sent",message_id="test-message",expected="attempted")
            restarted=SqueezeRepository(database)
            assert not await restarted.pending()
            assert await restarted.parent_sent(event["event_key"])
            assert (await restarted.alert(event["id"])).delivery_id=="test-message"
            assert (await restarted.counts())["sent"]==1
            await transaction.rollback()
    finally:
        await database.close()


async def test_only_one_squeeze_writer_and_release_reacquires_after_stop():
    database=Database(os.environ["DATABASE_URL"])
    try:
        repo=SqueezeRepository(database)
        async with repo.lease() as a:
            assert a is not None
            await verify_writer(a)
            async with repo.lease() as b:
                assert b is None
        async with repo.lease() as a:
            assert a is not None
            await verify_writer(a)
    finally:
        await database.close()
