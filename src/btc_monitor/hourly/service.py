from __future__ import annotations

import asyncio
import hashlib
import html
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.engine import evaluate, parameters
from btc_monitor.hourly.features import build_hour
from btc_monitor.hourly.outcomes import track
from btc_monitor.hourly.scoring import confidence
from btc_monitor.telegram import TelegramClient

log = structlog.get_logger()


def hour(time):
    return time.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def render_alert(row):
    snapshot = row.snapshot
    d, c = snapshot["diagnostics"], snapshot["candidate"]
    local = row.generated_at.astimezone(ZoneInfo("America/Toronto")).strftime("%m-%d %H:%M %Z")
    label = "观察 / WATCH" if row.tier == "WATCH" else "价格确认 / CONFIRMED"
    oi = "缺失" if d["oi_change_pct"] is None else f"{Decimal(d['oi_change_pct']):+.2f}%"
    funding = (
        "缺失"
        if d["funding_fraction_mean"] is None
        else f"{Decimal(d['funding_fraction_mean']) * 100:+.4f}%"
    )
    reasons = "\n".join("• " + html.escape(v) for v in c["reasons"])
    volume = "缺失" if d["volume_ratio"] is None else f"{Decimal(d['volume_ratio']):.2f}x"
    score = snapshot.get("confidence") or confidence(snapshot)
    score_label = f"{score['score']}/100" if score["score"] is not None else "无法评分（证据不足）"
    return (
        f"<b>BTC 1H {row.direction} — {label}</b>\n{local}\n"
        f"类型：{html.escape(c['kind'])}\n参考价：${row.entry:,.2f}\n"
        f"结构失效参考：${row.invalidation:,.2f}\n"
        f"证据置信分：{score_label}（规则分，非胜率）\n"
        f"现货 {snapshot['parameters']['flow_hours']}H ΔCVD："
        f"{Decimal(d['spot_delta_btc']):+.2f} BTC\n"
        f"合约 ΔCVD：{Decimal(d['futures_delta_btc']):+.2f} BTC\n"
        f"现货主动压力：{Decimal(d['spot_pressure']) * 100:+.2f}%\n"
        f"OI 1H：{oi}\n报告费率均值：{funding}\n"
        f"合约量比：{volume}\n"
        f"{html.escape(c['context'])}\n{reasons}\n"
        f"来源：{html.escape(row.source)} 固定交易所集合，非全市场\n"
        f"快照 ID：<code>{row.id}</code>\n"
        "<i>研究提醒，非买卖建议；未下单。费率未统一结算周期，OI不能确认多空仓位。</i>"
    )


class HourlyMonitor:
    def __init__(self, settings, repository):
        self.settings, self.repo = settings, repository
        self.telegram = TelegramClient(
            settings.hourly_telegram_enabled, settings.telegram_bot_token, settings.telegram_chat_id
        )
        self.task = None
        self.mutex = asyncio.Lock()
        self.started_at = datetime.now(UTC)
        self.last_success = self.last_error = self.last_evaluation = None
        self.waiting_for_writer = False
        self.bootstrap_done = False
        self.replay_task = None
        self.replay_state = {"state": "idle"}
        self.last_price_repair = None
        self.fingerprint = hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.parent.rglob("*.py")))
        ).hexdigest()

    async def start(self):
        if self.settings.hourly_enabled:
            self.task = asyncio.create_task(self._loop(), name="hourly-monitor")

    def health(self):
        return serializable(
            {
                "enabled": self.settings.hourly_enabled,
                "telegram_enabled": self.settings.hourly_telegram_enabled,
                "source": self.settings.hourly_source,
                "healthy": self.task is not None
                and not self.task.done()
                and self.last_success is not None
                and not self.last_error
                and datetime.now(UTC) - self.last_success < timedelta(minutes=3),
                "last_success_at": self.last_success,
                "last_error": self.last_error,
                "waiting_for_writer": self.waiting_for_writer,
                "last_evaluation": self.last_evaluation,
                "replay": self.replay_state,
                "parameters": parameters(self.settings),
            }
        )

    async def _loop(self):
        while True:
            try:
                async with self.mutex, self.repo.lease() as lease:
                    self.waiting_for_writer = lease is None
                    if lease is not None:
                        now = datetime.now(UTC)
                        last_end = hour(
                            now - timedelta(seconds=self.settings.hourly_decision_delay_seconds)
                        )
                        if not self.bootstrap_done:
                            await self.rebuild(
                                last_end - timedelta(hours=self.settings.hourly_context_hours),
                                last_end,
                                lease=lease,
                            )
                            self.bootstrap_done = True
                        else:
                            await self.process(last_end - timedelta(hours=1), now, True, lease)
                        await self.deliver_outbox(datetime.now(UTC), lease)
                        await self.update_outcomes(datetime.now(UTC))
                        self.last_success, self.last_error = datetime.now(UTC), None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.warning("hourly_monitor_retry", error_type=self.last_error)
            await asyncio.sleep(30)

    async def process(self, start, now, live, lease, force=False):
        end = start + timedelta(hours=1)
        source = self.settings.hourly_source
        cached = await self.repo.bars(source, start, end)
        if cached and not force and await self.repo.evaluated(source, start):
            return
        decision = end + timedelta(seconds=self.settings.hourly_decision_delay_seconds)
        if decision > now:
            return
        if not cached:
            rows = await self.repo.observations(start - timedelta(minutes=10), decision, source)
            if source == "coinalyze":
                metadata = await self.repo.metadata_history(decision)
                rows = list({r["id"]: r for r in rows + metadata}.values())
            bar = build_hour(rows, start, decision, source)
            await self.repo.save_bar(bar)
        history = await self.repo.bars(source, start - timedelta(hours=24), end)
        analysis = evaluate(history, self.settings)
        self.last_evaluation = {
            "bar_start": start,
            "blocked": analysis["blocked"],
            "candidate_count": len(analysis["candidates"]),
        }
        for candidate in analysis["candidates"]:
            entry, observed = await self.repo.entry_sample(now) if live else (None, None)
            basis = "fresh_bybit_ticker_at_generation" if entry else "historical_hour_close_no_fill"
            if entry is None:
                entry = Decimal(candidate["bar_close"])
            state = (
                "pending"
                if live and self.settings.hourly_telegram_enabled
                else "disabled"
                if live
                else "retrospective"
            )
            if live and observed is None:
                state = "missing_fresh_entry"
            if live and (
                (candidate["direction"] == "LONG" and entry <= Decimal(candidate["invalidation"]))
                or (
                    candidate["direction"] == "SHORT"
                    and entry >= Decimal(candidate["invalidation"])
                )
            ):
                state = "invalidated_before_delivery"
            if state == "pending" and await self.repo.cooling_down(
                source,
                start,
                candidate["direction"],
                candidate["tier"],
                self.settings.hourly_cooldown_hours,
            ):
                state = "suppressed_cooldown"
            alert_id = uuid4()
            ids = sorted({row_id for b in history for row_id in b["input_ids"]})
            snapshot = serializable(
                {
                    "schema_version": 1,
                    "code_sha256": self.fingerprint,
                    "parameters": parameters(self.settings),
                    "computed_at": now,
                    "decision_at": decision,
                    "causal_inputs": True,
                    "candidate": candidate,
                    "diagnostics": analysis["diagnostics"],
                    "history": history,
                    "input_ids": ids,
                    "entry_basis": basis,
                    "entry_observed_at": observed,
                    "reference_bar_close": candidate["bar_close"],
                    "origin": "live" if live else "retrospective",
                    "data_retention": "Immutable raw inputs retained; export includes all IDs",
                }
            )
            snapshot["confidence"] = confidence(snapshot)
            await self.repo.save_alert(
                {
                    "id": alert_id,
                    "source": source,
                    "bar_start": start,
                    "generated_at": now if live else decision,
                    "direction": candidate["direction"],
                    "tier": candidate["tier"],
                    "origin": "live" if live else "retrospective",
                    "entry": entry,
                    "invalidation": Decimal(candidate["invalidation"]),
                    "snapshot": snapshot,
                    "tracking_complete": False,
                    "delivery_state": state,
                }
            )
        await self.repo.mark_evaluated(source, start)

    async def deliver_outbox(self, now, lease):
        if not self.settings.hourly_telegram_enabled:
            return
        for alert in await self.repo.pending_deliveries():
            if now - alert.generated_at > timedelta(minutes=10):
                await self.repo.set_delivery(alert.id, "stale_delivery", expected="pending")
                continue
            price, observed = await self.repo.entry_sample(now)
            if price is None or observed is None:
                await self.repo.set_delivery(alert.id, "missing_fresh_delivery", expected="pending")
                continue
            if (alert.direction == "LONG" and price <= alert.invalidation) or (
                alert.direction == "SHORT" and price >= alert.invalidation
            ):
                await self.repo.set_delivery(
                    alert.id, "invalidated_before_delivery", expected="pending"
                )
                continue
            # Verify physical lock ownership before external effects.
            await lease.exec_driver_sql("SELECT 1")
            await lease.commit()
            if await self.repo.set_delivery(alert.id, "attempted", expected="pending"):
                try:
                    message_id = await self.telegram.send(render_alert(alert))
                except Exception:
                    await self.repo.set_delivery(alert.id, "uncertain")
                else:
                    await self.repo.set_delivery(alert.id, "sent", datetime.now(UTC), message_id)

    async def rebuild(self, start, end, lease):
        # Include prior context at the beginning of an explicit replay; market inputs stay pinned.
        cursor = start - timedelta(hours=24)
        while cursor < end:
            await lease.exec_driver_sql("SELECT 1")
            await lease.commit()
            await self.process(cursor, datetime.now(UTC), False, lease, force=True)
            cursor += timedelta(hours=1)

    async def request_replay(self, hours):
        if self.replay_task and not self.replay_task.done():
            return False

        async def job():
            self.replay_state = {"state": "running", "hours": hours}
            try:
                async with self.mutex, self.repo.lease() as lease:
                    if lease is None:
                        raise RuntimeError("Another hourly writer is active")
                    end = hour(
                        datetime.now(UTC)
                        - timedelta(seconds=self.settings.hourly_decision_delay_seconds)
                    )
                    await self.rebuild(end - timedelta(hours=hours), end, lease)
                    await self.update_outcomes(datetime.now(UTC))
                self.replay_state = {"state": "complete", "hours": hours}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.replay_state = {"state": "failed", "error": type(exc).__name__}

        self.replay_task = asyncio.create_task(job(), name="hourly-replay")
        return True

    async def update_outcomes(self, now, force=False, repair=True):
        alerts = await self.repo.outcome_candidates(self.settings.hourly_source, now, force)
        if not alerts:
            return
        for alert in alerts:
            end = min(now, alert.generated_at + timedelta(hours=24))
            if end <= alert.generated_at:
                continue
            begin = alert.generated_at.replace(second=0, microsecond=0)
            rows = await self.repo.observations(begin, end, "direct", price_only=True)
            result = track(alert, rows, now)
            # One bounded public price request per five minutes. Never fabricate prices,
            # use another venue, change region, or repair CVD with OHLC candles.
            missing = result["windows"]["24"]["missing_minutes"]
            if (
                result["windows"]["24"]["partial_start_minute"]
                and not result["windows"]["24"]["boundary_input_id"]
            ):
                missing = [{"start": begin.isoformat()}] + missing
            prior_repair = (alert.outcomes or {}).get("repair", {})
            previous_attempt = prior_repair.get("attempted_at")
            eligible = not previous_attempt or now - datetime.fromisoformat(
                previous_attempt
            ) >= timedelta(hours=6)
            if (
                repair
                and missing
                and eligible
                and (
                    self.last_price_repair is None
                    or now - self.last_price_repair >= timedelta(minutes=5)
                )
            ):
                from btc_monitor.archive.exchanges import PublicRest
                from btc_monitor.archive.history import HistoryLoader
                from btc_monitor.archive.store import ArchiveStore
                from btc_monitor.archive.types import Instrument

                self.last_price_repair = now
                rest = PublicRest(self.settings)
                repair_start = datetime.fromisoformat(missing[0]["start"])
                repair_end = min(
                    end.replace(second=0, microsecond=0), repair_start + timedelta(minutes=200)
                )
                report = {
                    "attempted_at": now.isoformat(),
                    "from": repair_start.isoformat(),
                    "to": repair_end.isoformat(),
                }
                try:
                    report["result"] = await asyncio.wait_for(
                        HistoryLoader(rest, ArchiveStore(self.repo.db)).prices_and_flow(
                            Instrument("bybit", "linear", "BTCUSDT"), repair_start, repair_end
                        ),
                        timeout=15,
                    )
                    report["state"] = "fetched"
                    rows = await self.repo.observations(begin, end, "direct", price_only=True)
                    result = track(alert, rows, datetime.now(UTC))
                    report["remaining_missing_minutes"] = len(
                        result["windows"]["24"]["missing_minutes"]
                    )
                except Exception as exc:
                    report.update(state="failed", error_type=type(exc).__name__)
                    if getattr(exc, "response", None) is not None:
                        report["http_status"] = exc.response.status_code
                finally:
                    await rest.close()
                result["repair"] = report
            elif prior_repair:
                result["repair"] = prior_repair
            await self.repo.save_outcomes(
                alert.id, result, now >= alert.generated_at + timedelta(hours=24, minutes=2)
            )

    async def stop(self):
        tasks = [t for t in (self.task, self.replay_task) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.telegram.close()
