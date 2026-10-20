import copy
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_hourly_sweep import setup_bars

from btc_monitor.config import Settings
from btc_monitor.hourly.engine import evaluate, parameters
from btc_monitor.hourly.service import HourlyMonitor, render_alert
from btc_monitor.runtime import MonitorRuntime


def confirmed_history():
    bars = setup_bars()
    bars[-1]["price"].update(open="104", high="104.5", low="99", close="100.5")
    next_bar = copy.deepcopy(bars[-1])
    next_bar["start"] = bars[-1]["end"]
    next_bar["end"] = (datetime.fromisoformat(next_bar["start"]) + timedelta(hours=1)).isoformat()
    next_bar["decision_at"] = (datetime.fromisoformat(next_bar["end"]) + timedelta(minutes=3)).isoformat()
    next_bar["price"].update(open="100.5", high="105", low="98.8", close="104.8")
    next_bar["aggregated"]["linear"]["delta_btc"] = "20"
    bars.append(next_bar)
    for b in bars:
        b.update(causal=True, closed=True, strategy_eligible=True)
    return bars


def test_v5_live_adapter_is_causal_and_matches_next_candle_rule():
    settings = Settings(_env_file=None, hourly_strategy="sweep_v5")
    bars = confirmed_history()
    result = evaluate(bars, settings)
    assert result["version"] == "h1-v1-cvd-transition-sweep-v5"
    c = result["candidates"][0]
    assert c["morphology"]["confirmation_start"] == bars[-1]["start"]
    assert c["morphology"]["cvd_transition"]["futures_state"] == "rotated_toward_signal"
    assert parameters(settings)["active_signal_kinds"] == ["v1_cvd_transition_sweep_v5"]
    bars[-2]["causal"] = False
    assert evaluate(bars, settings)["blocked"] == "non_causal_inputs"
    bars[-2]["causal"] = True
    bars[-1]["closed"] = False
    assert evaluate(bars, settings)["blocked"] == "price_gap_or_unclosed_bar"


@pytest.mark.asyncio
async def test_5m_client_off_while_hourly_client_on():
    settings = Settings(_env_file=None, telegram_enabled=True, five_minute_telegram_enabled=False,
                        hourly_enabled=True, hourly_telegram_enabled=True, hourly_strategy="sweep_v5")
    runtime = MonitorRuntime(settings, SimpleNamespace())
    monitor = HourlyMonitor(settings, SimpleNamespace())
    try:
        assert not runtime.telegram.enabled
        assert monitor.telegram.enabled
    finally:
        await runtime.telegram.close()
        await runtime.rest.close()
        await monitor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("live,late,expected", [(True, False, "pending"), (False, False, "retrospective"), (True, True, "stale_delivery")])
async def test_v5_signal_snapshot_and_history_never_notifications(live, late, expected):
    bars = confirmed_history()
    settings = Settings(_env_file=None, hourly_strategy="sweep_v5", hourly_telegram_enabled=True)
    start = datetime.fromisoformat(bars[-1]["start"])
    now = datetime.fromisoformat(bars[-1]["decision_at"]) + (timedelta(minutes=11) if late else timedelta())
    repo = SimpleNamespace(bars=AsyncMock(side_effect=[[bars[-1]], bars]), evaluated=AsyncMock(return_value=False),
        entry_sample=AsyncMock(return_value=(Decimal("104.8"), now)), cooling_down=AsyncMock(return_value=False),
        save_alert=AsyncMock(), mark_evaluated=AsyncMock())
    monitor = HourlyMonitor(settings, repo)
    try:
        await monitor.process(start, now, live, None)
        row = repo.save_alert.call_args.args[0]
        assert row["delivery_state"] == expected
        assert row["snapshot"]["candidate"]["kind"] == "v1_cvd_transition_sweep_v5"
        assert row["snapshot"]["confidence"] == row["snapshot"]["candidate"]["evidence_score"]
        assert "V5" in render_alert(SimpleNamespace(**row))
        if expected == "pending":
            assert repo.cooling_down.call_args.kwargs["kind"] == "v1_cvd_transition_sweep_v5"
    finally:
        await monitor.stop()


@pytest.mark.asyncio
async def test_v5_outbox_supersedes_v1_and_keeps_only_v5():
    bars = confirmed_history()
    settings = Settings(_env_file=None, hourly_strategy="sweep_v5", hourly_telegram_enabled=True)
    c = evaluate(bars, settings)["candidates"][0]
    now = datetime.fromisoformat(bars[-1]["decision_at"])
    old = SimpleNamespace(id=uuid4(), snapshot={"candidate": {"kind": "sweep_split_cvd"}})
    new = SimpleNamespace(id=uuid4(), generated_at=now, entry=Decimal("104.8"), invalidation=Decimal(c["invalidation"]),
        direction="LONG", tier="CONFIRMED", source="coinalyze", snapshot={"candidate": c, "parameters": parameters(settings), "diagnostics": {}, "history": bars})
    repo = SimpleNamespace(pending_deliveries=AsyncMock(return_value=[old, new]), set_delivery=AsyncMock(return_value=True),
        entry_sample=AsyncMock(return_value=(new.entry, now)))
    monitor = HourlyMonitor(settings, repo)
    monitor.telegram.send = AsyncMock(return_value="test_id")
    try:
        await monitor.deliver_outbox(now, SimpleNamespace(exec_driver_sql=AsyncMock(), commit=AsyncMock()))
        repo.set_delivery.assert_any_await(old.id, "superseded_strategy", expected="pending")
        assert monitor.telegram.send.await_count == 1
        assert "V5" in monitor.telegram.send.call_args.args[0]
    finally:
        await monitor.stop()
