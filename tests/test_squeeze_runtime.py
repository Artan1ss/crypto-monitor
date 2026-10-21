import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC,datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from btc_monitor.config import Settings
from btc_monitor.squeeze.engine import FEEDS,FUTURES,annotate,VERSION
from btc_monitor.squeeze.service import advance,SqueezeMonitor,render_signal


def bars(count=25):
    result=[]
    for i in range(count):
        start=datetime(2026,1,1,tzinfo=UTC)+timedelta(minutes=15*i)
        result.append({"start":start.isoformat(),"end":(start+timedelta(minutes=15)).isoformat(),
            "closed":True,"complete":True,"price":{"open":"100","high":"101","low":"99","close":"100"},
            "flow":{f:{"volume":"100","delta":"-3" if ":linear:" in f else "3"} for f in FEEDS},
            "oi":{f:str(1000+i*10) for f in FUTURES},"oi_total":str((1000+i*10)*3),
            "spot":{"volume":"300","delta":"9"},"linear":{"volume":"300","delta":"-9"},
            "funding":{f:"-0.001" for f in FUTURES},"input_ids":[i],"raw_fingerprints":[str(i)]})
    return result


def cfg(**extra):
    return Settings(_env_file=None,squeeze_enabled=True,squeeze_telegram_enabled=True,
        telegram_enabled=True,telegram_bot_token="test-only",telegram_chat_id="test-only",**extra)


def after(bar,seconds=181):
    return datetime.fromisoformat(bar["end"])+timedelta(seconds=seconds)


def live_case():
    data=bars(19)
    state,_=advance({},data[:14],after(data[13]),cfg())
    state,events=advance(state,data[14:15],after(data[14]),cfg())
    return data,state,events[0]


def test_stream_json_restart_matches_full_prefix_rules_and_no_mutation():
    data=bars(80);before=copy.deepcopy(data)
    expected,_=annotate(data)
    state={};actual=[]
    for chunk in (data[:17],data[17:31],data[31:]):
        found,_=annotate(chunk,state)
        actual+=found
        state=json.loads(json.dumps(state))
    assert actual==expected and data==before
    assert len(state["history"])==15


def test_first_boot_is_historical_only_and_new_closed_bar_is_pending_once():
    data,state,event=live_case()
    assert event["origin"]=="live" and event["delivery_state"]=="pending"
    assert event["payload"]["snapshot"]["raw_input_fingerprints"]
    assert event["payload"]["decision_at"]==after(data[14]).isoformat()
    _,duplicates=advance(json.loads(json.dumps(state)),data[14:],after(data[-1]),cfg())
    assert not duplicates
    _,cold=advance({},data,after(data[-1]),cfg())
    assert cold and all(e["delivery_state"]=="historical_no_send" for e in cold)


def test_partial_forming_delay_gap_and_repair_never_force_alerts():
    data=bars(16)
    state,_=advance({},data[:14],after(data[13]),cfg())
    _,early=advance(state,data[14:15],after(data[14],120),cfg())
    assert not early
    incomplete=copy.deepcopy(data[14]);incomplete["complete"]=False
    retry,found=advance(state,[incomplete],after(data[14]),cfg())
    assert not found and retry["last_end"]==state["last_end"]
    repaired,found=advance(retry,data[14:15],after(data[14],240),cfg())
    assert found and repaired["blocked"] is None
    expired,found=advance(state,[incomplete],after(data[14],900),cfg())
    assert not found and expired["blocked"]=="expired_incomplete_15m_gap"


def test_catchup_after_downtime_never_creates_stale_telegram_outbox():
    data=bars(16)
    state,_=advance({},data[:14],after(data[13]),cfg())
    _,events=advance(state,data[14:],after(data[-1],1800),cfg())
    assert events and all(e["delivery_state"]=="historical_no_send" for e in events)


class Repo:
    def __init__(self,events):
        self.events={e["id"]:copy.deepcopy(e) for e in events}
        self.parent_ok=False
    async def pending(self):
        return [SimpleNamespace(**e) for e in self.events.values() if e["delivery_state"]=="pending"]
    async def delivery(self,identity,state,at=None,message_id=None,expected="pending"):
        e=self.events[identity]
        if e["delivery_state"]!=expected:
            return False
        e.update(delivery_state=state,delivery_id=message_id,delivery_at=at)
        return True
    async def parent_sent(self,key):
        return self.parent_ok


async def test_at_most_once_delivery_uncertain_does_not_retry_and_stale_suppressed():
    data,state,event=live_case()
    repo=Repo([event]);monitor=SqueezeMonitor(cfg(),repo,None)
    lease=SimpleNamespace(execute=AsyncMock(),commit=AsyncMock(),scalar=AsyncMock(return_value=True))
    monitor.telegram.send=AsyncMock(return_value="123")
    now=after(data[14],200)
    try:
        await monitor.deliver(now,lease)
        await monitor.deliver(now,lease)
        assert monitor.telegram.send.await_count==1
        assert repo.events[event["id"]]["delivery_state"]=="sent"
        repo.events[event["id"]]["delivery_state"]="pending"
        monitor.telegram.send=AsyncMock(side_effect=RuntimeError("network secret must not leak"))
        await monitor.deliver(now,lease)
        await monitor.deliver(now,lease)
        assert monitor.telegram.send.await_count==1
        assert repo.events[event["id"]]["delivery_state"]=="uncertain"
        repo.events[event["id"]]["delivery_state"]="pending"
        await monitor.deliver(now+timedelta(hours=1),lease)
        assert repo.events[event["id"]]["delivery_state"]=="stale_delivery"
    finally:
        await monitor.stop()


async def test_release_requires_notified_parent_and_message_is_not_an_order():
    data,state,event=live_case()
    event["phase"]="RELEASE"
    event["payload"].update(phase="RELEASE",parent_id="unnotified-watch")
    repo=Repo([event]);monitor=SqueezeMonitor(cfg(),repo,None)
    monitor.telegram.send=AsyncMock(return_value="456")
    lease=SimpleNamespace(execute=AsyncMock(),commit=AsyncMock(),scalar=AsyncMock(return_value=True))
    try:
        await monitor.deliver(after(data[14],200),lease)
        monitor.telegram.send.assert_not_awaited()
        assert repo.events[event["id"]]["delivery_state"]=="parent_not_notified"
        text=render_signal(SimpleNamespace(**event))
        assert "非事前预警" in text and "未下单" in text and "非释放成功率" in text
        assert "test-only" not in text
    finally:
        await monitor.stop()


async def test_disabled_and_claimed_events_cannot_be_sent():
    data,state,event=live_case()
    repo=Repo([event]);monitor=SqueezeMonitor(cfg(),repo,None)
    monitor.telegram.enabled=False
    monitor.telegram.send=AsyncMock()
    try:
        await monitor.deliver(after(data[14],200),SimpleNamespace())
        monitor.telegram.send.assert_not_awaited()
        assert repo.events[event["id"]]["delivery_state"]=="disabled"
    finally:
        await monitor.stop()


def test_opt_in_requires_collector_and_global_telegram():
    with pytest.raises(ValueError,match="ARCHIVE"):
        Settings(_env_file=None,squeeze_enabled=True).validate_runtime()
    with pytest.raises(ValueError,match="SQUEEZE_TELEGRAM"):
        Settings(_env_file=None,squeeze_telegram_enabled=True).validate_runtime()


async def test_native_collection_shared_source_bootstrap_never_sends_and_units_fail_closed(monkeypatch):
    import btc_monitor.squeeze.service as service
    from btc_monitor.archive.types import Observation,Instrument
    from btc_monitor.archive.coinalyze import ProviderMarket
    data=bars(48)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime(2026,1,1,12,3,1,tzinfo=UTC)
    monkeypatch.setattr(service,"datetime",Clock)
    now=Clock.now()
    markets=[ProviderMarket(Instrument(*f.split(":")),f,{"has_buy_sell_data":True},"base") for f in FEEDS]
    proofs={f:{"provider_symbol":f,"feed":f,"verified_at":(now-timedelta(hours=1)).isoformat(),
        "evidence":{"verified":True,"matched_minutes":3,"max_relative_error":"0"}} for f in FEEDS if ":spot:" in f}
    async def observations(market,endpoint,start,end,interval):
        assert interval=="15min"
        rows=[]
        for b in data:
            a,z=datetime.fromisoformat(b["start"]),datetime.fromisoformat(b["end"])
            if not start<=a<z<=end:
                continue
            p={"volume_unit":"base","provider":"coinalyze",**b["price"]}
            if endpoint=="ohlcv-history":
                dataset="provider_flow";p.update(total_native="100",delta_btc=b["flow"][market.instrument.key]["delta"])
            elif endpoint=="open-interest-history":
                dataset="provider_oi";p["reported_oi_btc"]=b["oi"][market.instrument.key]
            else:
                dataset="provider_funding";p["rate_fraction"]="-0.001"
            i=market.instrument
            rows.append(Observation(i.venue,i.market,i.symbol,dataset,900,a,z,now,"coinalyze_history","test","complete",p))
        return rows
    metadata=Observation("coinalyze","metadata","BTC","provider_metadata",0,now,now,now,
        "coinalyze_history","test","complete",{"provider":"coinalyze","unit_verifications":proofs})
    source=SimpleNamespace(markets=markets,unit_verifications=proofs,observations=AsyncMock(side_effect=observations),
        metadata_observation=lambda:metadata)
    repo=Repo([])
    repo.saved=None
    @asynccontextmanager
    async def lease():
        yield SimpleNamespace(scalar=AsyncMock(return_value=True),commit=AsyncMock())
    repo.lease=lease
    async def read(version):
        return copy.deepcopy(repo.saved)
    async def save(version,state,events):
        repo.saved=copy.deepcopy(state)
        for e in events:
            repo.events.setdefault(e["id"],copy.deepcopy(e))
    repo.state=read;repo.save=save
    archive=SimpleNamespace(append=AsyncMock(return_value=1))
    monitor=SqueezeMonitor(cfg(),repo,archive)
    monitor.telegram.send=AsyncMock(return_value="must-not-send")
    try:
        await monitor.collect(source)
        assert monitor.last_error is None
        assert repo.saved["last_end"]==data[-1]["end"]
        assert len(repo.saved["engine"]["history"])==15
        assert repo.events and all(e["origin"]=="historical" for e in repo.events.values())
        monitor.telegram.send.assert_not_awaited()
        assert all(o.source_type=="coinalyze_squeeze" for o in archive.append.call_args.args[0])
        await monitor.collect(source)
        assert source.observations.await_count==12
        source.unit_verifications={}
        await monitor.collect(source)
        assert monitor.last_error=="spot_unit_evidence_not_ready"
        assert source.observations.await_count==12
    finally:
        await monitor.stop()
