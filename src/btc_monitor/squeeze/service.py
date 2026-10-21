"""Causal, closed-15m runtime around V2 morphology; no orders or historical TG."""
import copy
import hashlib
import html
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text

from btc_monitor.archive.types import serializable
from btc_monitor.archive.units import proof_valid
from btc_monitor.squeeze.engine import VERSION, PARAMETERS, FEEDS, FUTURES, annotate, build_bars
from btc_monitor.telegram import TelegramClient

log=structlog.get_logger()
RUNTIME_VERSION=VERSION+"-causal-runtime-v1"


def quarter(at):
    at=at.astimezone(UTC)
    return at.replace(minute=at.minute//15*15,second=0,microsecond=0)


async def verify_writer(lease):
    owned=await lease.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND classid=0 AND objid=724196309 AND granted)"))
    await lease.commit()
    if not owned:
        raise RuntimeError("squeeze_writer_lease_lost")


def advance(state, bars, now, settings):
    state=copy.deepcopy(state or {})
    state.setdefault("notify_after",quarter(now).isoformat())
    engine=state.setdefault("engine",{})
    events=[]
    for bar in bars:
        finish=datetime.fromisoformat(bar["end"])
        if state.get("last_end") and finish<=datetime.fromisoformat(state["last_end"]):
            continue
        decision=finish+timedelta(seconds=settings.squeeze_decision_delay_seconds)
        if now<decision:
            break
        deadline=decision+timedelta(seconds=settings.squeeze_max_delivery_age_seconds)
        if not bar["complete"] and now<=deadline:
            state["blocked"]="incomplete_latest_15m_waiting_for_repair"
            break
        signals,_=annotate([bar],engine)
        state["last_end"]=bar["end"]
        state["blocked"]=None if bar["complete"] else "expired_incomplete_15m_gap"
        for signal in signals:
            live=finish>datetime.fromisoformat(state["notify_after"]) and now<=deadline
            payload=copy.deepcopy(signal)
            payload["decision_at"]=now.isoformat()
            payload["theoretical_decision_at"]=decision.isoformat()
            payload["runtime_version"]=RUNTIME_VERSION
            payload["causal_basis"]="inputs received and saved before actual decision; no historical availability claim"
            payload["snapshot"]["raw_input_fingerprints"]=sorted({f for b in payload["snapshot"]["history"] for f in b.get("raw_fingerprints",[])})
            payload["snapshot"]["unit_metadata_fingerprint"]=state.get("unit_metadata_fingerprint")
            payload["snapshot"]["unit_proofs"]=copy.deepcopy(state.get("unit_proofs",{}))
            events.append({"id":uuid4(),"event_key":VERSION+":"+signal["id"],
                "bar_start":datetime.fromisoformat(signal["bar_start"]),"generated_at":now,
                "direction":signal["direction"],"phase":signal["phase"],"origin":"live" if live else "historical",
                "payload":serializable(payload),"delivery_state":"pending" if live and settings.squeeze_telegram_enabled else "disabled" if live else "historical_no_send"})
    state["processed_at"]=now.isoformat()
    return serializable(state),events


def render_signal(row):
    s=row.payload;f=s["diagnostics"]
    upward=s["direction"]=="SHORT_SQUEEZE"
    direction="轧空风险 · 向上" if upward else "多头挤压风险 · 向下"
    stage="观察预警 / WATCH" if s["phase"]=="WATCH" else "释放迹象 / RELEASE（非事前预警）"
    kind="相对背离" if s["divergence_type"]=="relative_resilience" else "现货/合约反向背离"
    pct=lambda v: f"{float(v)*100:+.2f}%"
    local=row.generated_at.astimezone(ZoneInfo("America/Toronto")).strftime("%m-%d %H:%M:%S %Z")
    score_note="原始观察证据分，非释放成功率" if s["phase"]=="RELEASE" else "规则证据分，非胜率"
    return (
        f"<b>BTC 15m V2 — {direction}</b>\n<b>{stage}</b>\n{local}\n"
        f"类型：{kind}\n证据分：{s['confidence']['score']}/100（{score_note}）\n"
        f"判定K线收盘（UTC）：{html.escape(s['evaluated_at'])}\n"
        f"价格观察值：${float(s['price_reference']):,.2f}（非入场）\n"
        f"现货3H ΔCVD：{float(f['spot']['delta']):+.2f} BTC / 压力 {pct(f['spot']['pressure'])}\n"
        f"合约3H ΔCVD：{float(f['linear']['delta']):+.2f} BTC / 压力 {pct(f['linear']['pressure'])}\n"
        f"方向化压力差：{float(f['signed_pressure_gap'])*100:+.2f}个百分点\n"
        f"OI 3H / 1H：{pct(f['oi_growth'])} / {pct(f['oi_1h'])}\n"
        f"OI递增步长：{f['positive_oi_steps']}/12\n"
        f"费率报告均值：{format(float(f['funding_mean'])*100,'+.4f')+'%' if f['funding_mean'] is not None else '缺失'}\n"
        f"证据ID：<code>{row.id}</code>\n"
        "<i>观察提醒，不是交易指令，未下单。固定六源非全市场；OI不能证明新增多空或真实强平。"
        "释放迹象是突破与去杠杆的代理，不能倒填成提前预警。</i>"
    )


class SqueezeMonitor:
    def __init__(self,settings,repository,archive_store):
        self.settings,self.repo,self.archive=settings,repository,archive_store
        self.telegram=TelegramClient(settings.squeeze_enabled and settings.squeeze_telegram_enabled and settings.telegram_enabled,
                                     settings.telegram_bot_token,settings.telegram_chat_id)
        self.last_success=self.last_error=self.last_decision=None
        self.last_delivery_error=self.last_sent_at=None
        self.waiting_for_writer=False
        self.fingerprint=hashlib.sha256(Path(__file__).with_name("engine.py").read_bytes()+Path(__file__).read_bytes()).hexdigest()

    def health(self):
        return serializable({"enabled":self.settings.squeeze_enabled,
            "telegram_enabled":self.telegram.enabled,"version":RUNTIME_VERSION,"rules":PARAMETERS,
            "healthy":self.settings.squeeze_enabled and self.last_success is not None and not self.last_error and not self.last_delivery_error
                and datetime.now(UTC)-self.last_success<timedelta(seconds=self.settings.coinalyze_poll_seconds+180),
            "last_success_at":self.last_success,"last_error":self.last_error,"last_decision":self.last_decision,
            "waiting_for_writer":self.waiting_for_writer,"source_sha256":self.fingerprint,
            "last_delivery_error":self.last_delivery_error,"last_sent_at":self.last_sent_at,
            "collection":"shared Coinalyze limiter, native 15min, fixed six feeds",
            "orders_enabled":False})

    async def collect(self,source):
        if not self.settings.squeeze_enabled:
            return
        try:
            async with self.repo.lease() as lease:
                self.waiting_for_writer=lease is None
                if lease is None:
                    return
                now=datetime.now(UTC)
                selected={m.instrument.key:m for m in source.markets if m.instrument.key in FEEDS}
                if set(selected)!=set(FEEDS) or any(m.volume_unit!="base" for m in selected.values()):
                    self.last_error="fixed_cohort_or_units_incomplete";return
                for feed,m in selected.items():
                    if ":spot:" in feed:
                        proof=source.unit_verifications.get(feed,{})
                        if proof.get("provider_symbol")!=m.provider_symbol or not proof_valid(proof,now):
                            self.last_error="spot_unit_evidence_not_ready";return
                state=await self.repo.state(RUNTIME_VERSION) or {}
                self.last_decision={"latest_end":state.get("last_end"),"blocked":state.get("blocked"),"created":0}
                end=quarter(now-timedelta(seconds=self.settings.squeeze_decision_delay_seconds))
                if not state.get("last_end") or datetime.fromisoformat(state["last_end"])<end:
                    start=max(end-timedelta(hours=12),
                              datetime.fromisoformat(state["last_end"]) if state.get("last_end") else end-timedelta(hours=12))
                    rows=[]
                    for feed in FEEDS:
                        endpoints=["ohlcv-history"]+(["open-interest-history","funding-rate-history"] if feed in FUTURES else [])
                        for endpoint in endpoints:
                            rows.extend(await source.observations(selected[feed],endpoint,start,end,"15min"))
                    # Keep native squeeze rows out of the unchanged 1H provider
                    # input selector; this source identity is independently auditable.
                    rows=[replace(r,source_type="coinalyze_squeeze") for r in rows]
                    metadata=replace(source.metadata_observation(),source_type="coinalyze_squeeze")
                    # Raw evidence must commit before decisions/outbox. No credential-bearing URLs are saved.
                    await self.archive.append(rows+[metadata])
                    decision_now=datetime.now(UTC)
                    if any(not proof_valid(source.unit_verifications.get(feed,{}),decision_now) for feed in FEEDS if ":spot:" in feed):
                        self.last_error="spot_unit_evidence_expired_during_fetch";return
                    bars=build_bars(rows,{"requested_start":start.isoformat(),"requested_end":end.isoformat()})
                    for b in bars:
                        b["raw_fingerprints"]=[rows[i].values()["fingerprint"] for i in b["input_ids"]]
                        b["observed_at"]=decision_now.isoformat()
                    state.update(unit_metadata_fingerprint=metadata.values()["fingerprint"],
                                 unit_proofs=serializable(source.unit_verifications))
                    state,events=advance(state,bars,decision_now,self.settings)
                    await verify_writer(lease)
                    await self.repo.save(RUNTIME_VERSION,state,events)
                    self.last_decision={"latest_end":state.get("last_end"),"blocked":state.get("blocked"),"created":len(events)}
                await self.deliver(datetime.now(UTC),lease)
                self.last_success=datetime.now(UTC);self.last_error=None
        except Exception as exc:
            self.last_error=type(exc).__name__
            log.warning("squeeze_runtime_retry",error_type=self.last_error)

    async def deliver(self,now,lease):
        for row in await self.repo.pending():
            s=row.payload
            decision=datetime.fromisoformat(s["theoretical_decision_at"])
            if now-decision>timedelta(seconds=self.settings.squeeze_max_delivery_age_seconds):
                await self.repo.delivery(row.id,"stale_delivery",now);continue
            if not self.telegram.enabled:
                await self.repo.delivery(row.id,"disabled",now);continue
            if s["phase"]=="RELEASE" and not await self.repo.parent_sent(VERSION+":"+s["parent_id"]):
                await self.repo.delivery(row.id,"parent_not_notified",now);continue
            # No retry after claiming or uncertain network delivery. This is at-most-once,
            # not an impossible exactly-once claim across PostgreSQL and Telegram.
            await verify_writer(lease)
            if not await self.repo.delivery(row.id,"attempted",now,expected="pending"):
                continue
            try:
                message=await self.telegram.send(render_signal(row))
                if message is None:
                    raise RuntimeError("telegram_disabled_after_claim")
            except Exception as exc:
                self.last_delivery_error=type(exc).__name__
                await self.repo.delivery(row.id,"uncertain",datetime.now(UTC),expected="attempted")
                log.warning("squeeze_delivery_uncertain",alert_id=str(row.id))
                continue
            await self.repo.delivery(row.id,"sent",datetime.now(UTC),message,expected="attempted")
            self.last_delivery_error=None;self.last_sent_at=datetime.now(UTC)
            log.info("squeeze_telegram_sent",alert_id=str(row.id),phase=row.phase)

    async def stop(self):
        await self.telegram.close()
