import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from test_hourly import bars

from btc_monitor.config import Settings
from btc_monitor.hourly.api import routes
from btc_monitor.hourly.engine import evaluate, parameters
from btc_monitor.hourly.scoring import confidence
from btc_monitor.hourly.service import HourlyMonitor, render_alert
from btc_monitor.hourly.sweep import evaluate_sweep
from btc_monitor.hourly.sweep_research import dashboard_payload, replay


def setup_bars():
    data = bars()
    for b in data:
        b["causal"] = True
        b["aggregated"]["spot"]["delta_btc"] = "1"
        b["aggregated"]["linear"]["delta_btc"] = "-2"
    data[18]["price"].update(open="103", high="105", low="100", close="104")
    data[-1]["price"].update(open="102", high="105", low="99", close="103")
    return data


def test_sweep_split_cvd_complete_diagnostics_and_no_mutation():
    data = setup_bars()
    before = copy.deepcopy(data)
    result = evaluate_sweep(data)
    c = result["candidates"][0]
    assert (c["direction"], c["kind"]) == ("LONG", "sweep_split_cvd")
    d = c["sweep_diagnostics"]
    assert d["pivot_start"] == data[18]["start"]
    assert d["pivot_confirmed_at"] == data[19]["end"]
    assert d["flows"]["spot"]["pivot_to_now_delta_btc"] == "6"
    assert d["flows"]["linear"]["pivot_to_now_delta_btc"] == "-12"
    assert d["flows"]["spot"]["normalizer_btc"] == "360"
    assert not d["cvd_reset"]
    assert data == before


def test_no_reclaim_no_signal_and_previous_piercing_consumes_anchor():
    data = setup_bars()
    data[-1]["price"]["close"] = "99.5"
    assert not evaluate_sweep(data)["candidates"]
    data = setup_bars()
    data[-2]["price"]["low"] = "99.8"
    assert not evaluate_sweep(data)["candidates"]


def test_mirror_short_and_flat_spot_allowed_not_flat_futures():
    data = setup_bars()
    for b in data:
        p = b["price"]
        b["price"] = {
            "open": str(200 - float(p["open"])),
            "close": str(200 - float(p["close"])),
            "high": str(200 - float(p["low"])),
            "low": str(200 - float(p["high"])),
        }
        b["aggregated"]["spot"]["delta_btc"] = "0"
        b["aggregated"]["linear"]["delta_btc"] = "2"
    assert evaluate_sweep(data)["candidates"][0]["direction"] == "SHORT"
    for b in data:
        b["aggregated"]["linear"]["delta_btc"] = "0"
    assert not evaluate_sweep(data)["candidates"]


def test_gaps_changed_cohort_and_unknown_units_block():
    data = setup_bars()
    data[21]["complete"] = False
    assert not evaluate_sweep(data)["candidates"]
    data = setup_bars()
    del data[21]
    assert not evaluate_sweep(data)["candidates"]
    data = setup_bars()
    data[-1]["expected_feeds"] = ["other"]
    assert evaluate_sweep(data)["blocked"] == "changed_cohort"
    data = setup_bars()
    data[21]["aggregated"]["spot"]["volume_btc"] = "0.0001"
    assert not evaluate_sweep(data)["candidates"]


def test_retrospective_cannot_enter_live_strategy_and_unclosed_blocks():
    data = setup_bars()
    for b in data:
        b.update(causal=False, strategy_eligible=False)
    assert evaluate_sweep(data)["blocked"] == "non_causal_inputs"
    assert evaluate_sweep(data, research=True)["candidates"]
    data[-1]["closed"] = False
    assert not evaluate_sweep(data, research=True)["candidates"]


def test_pivot_requires_prior_right_confirmation_and_future_cannot_change_prefix():
    data = setup_bars()
    data[18]["price"]["low"] = "102"
    data[-2]["price"].update(open="103", high="105", low="100", close="104")
    assert not evaluate_sweep(data)["candidates"]
    data = setup_bars()
    first = evaluate_sweep(data)
    later = copy.deepcopy(data[-1])
    later["price"]["close"] = "104"
    assert evaluate_sweep((data + [later])[:25]) == first


def test_engine_scoring_and_telegram_use_pivot_interval_not_rolling_window():
    data = setup_bars()
    analysis = evaluate(data, Settings(_env_file=None))
    c = next(c for c in analysis["candidates"] if c["kind"] == "sweep_split_cvd")
    assert len([x for x in analysis["candidates"] if x["direction"] == "LONG"]) == 1
    snapshot = {
        "diagnostics": analysis["diagnostics"],
        "candidate": c,
        "parameters": {"flow_hours": 6},
        "history": data,
    }
    assert confidence(snapshot) == c["evidence_score"]
    row = SimpleNamespace(
        snapshot=snapshot,
        generated_at=datetime.fromisoformat(data[-1]["end"]),
        tier="CONFIRMED",
        direction="LONG",
        entry=100,
        invalidation=99,
        source="coinalyze",
        id="test",
    )
    text = render_alert(row)
    assert "摆动锚点" in text and "+6.00 BTC" in text and "不是影线瞬间" in text


def test_research_reviews_do_not_read_gaps_as_order_or_direction_success():
    data = setup_bars()
    for b in data:
        b["closed"] = True
    future = copy.deepcopy(data[-1])
    start = datetime.fromisoformat(data[-1]["end"]) + timedelta(hours=1)
    future.update(start=start.isoformat(), end=(start + timedelta(hours=1)).isoformat())
    future["price"].update(open="103", low="103", high="120", close="119")
    report = replay({"bars": data + [future]})
    signal = report["signals"][0]
    assert signal["reviews"]["1"]["touch_order"] == "unknown_due_to_gaps"
    assert signal["reviews"]["1"]["direction_score"] is None
    assert not signal["snapshot"]["causal_inputs"]


@pytest.mark.asyncio
async def test_local_research_endpoint_and_production_disabled(tmp_path):
    data = setup_bars()
    for b in data:
        b["closed"] = True
    feed = SimpleNamespace(view=AsyncMock(return_value={"bars": data}))
    app = FastAPI()
    app.include_router(routes(None, None, local_reports=tmp_path, live_feed=feed))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/hourly/sweep-research")
    assert response.status_code == 200
    payload = response.json()
    assert payload["sweep_research"]
    assert payload["signals"][0]["delivery_state"] == "retrospective"
    assert not payload["signals"][0]["snapshot"]["causal_inputs"]
    assert payload["signals"][0]["research_review"]["24"]["direction_score"] is None
    production = FastAPI()
    production.include_router(routes(None, None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=production), base_url="http://test"
    ) as client:
        assert (await client.get("/hourly/sweep-research")).status_code == 404


def test_market_waiting_for_metadata_blocks_without_crashing_and_chart_preserves_score():
    data = setup_bars()
    del data[0]["expected_feeds"]
    assert evaluate_sweep(data)["blocked"] == "changed_cohort"
    data = setup_bars()
    for b in data:
        b["closed"] = True
    report = replay({"bars": data})
    payload = dashboard_payload(report)
    signal = payload["signals"][0]
    assert signal["confidence"] == report["signals"][0]["candidate"]["evidence_score"]
    assert signal["snapshot"]["candidate"]["sweep_diagnostics"]["compared_hours"] == 6


def test_default_replaces_pressure_candidates_and_mode_is_validated():
    settings = Settings(_env_file=None)
    assert settings.hourly_strategy == "sweep"
    assert not evaluate(bars(), settings)["candidates"]
    assert evaluate(bars(), settings.model_copy(update={"hourly_strategy": "pressure"}))[
        "candidates"
    ]
    data = setup_bars()
    result = evaluate(data, settings)
    assert result["version"] == "h1-sweep-split-cvd-v1"
    assert all(c["kind"] == "sweep_split_cvd" for c in result["candidates"])
    assert parameters(settings)["active_signal_kinds"] == ["sweep_split_cvd"]
    with pytest.raises(ValueError, match="HOURLY_STRATEGY"):
        Settings(_env_file=None, hourly_strategy="typo")


@pytest.mark.asyncio
async def test_replacement_cancels_legacy_outbox_and_sends_only_new_sweep():
    from decimal import Decimal
    from uuid import uuid4

    data = setup_bars()
    settings = Settings(_env_file=None, hourly_strategy="sweep", hourly_telegram_enabled=True)
    analysis = evaluate(data, settings)
    c = analysis["candidates"][0]
    now = datetime.fromisoformat(data[-1]["end"])
    old = SimpleNamespace(id=uuid4(), snapshot={"candidate": {"kind": "cvd_divergence"}})
    new = SimpleNamespace(
        id=uuid4(),
        generated_at=now,
        entry=Decimal(c["bar_close"]),
        invalidation=Decimal(c["invalidation"]),
        direction=c["direction"],
        tier=c["tier"],
        source="coinalyze",
        snapshot={
            "history": data,
            "candidate": c,
            "diagnostics": analysis["diagnostics"],
            "parameters": parameters(settings),
        },
    )
    repo = SimpleNamespace(
        pending_deliveries=AsyncMock(return_value=[old, new]),
        set_delivery=AsyncMock(return_value=True),
        entry_sample=AsyncMock(return_value=(new.entry, now)),
    )
    monitor = HourlyMonitor(settings, repo)
    monitor.telegram.send = AsyncMock(return_value="test_message")
    lease = SimpleNamespace(exec_driver_sql=AsyncMock(), commit=AsyncMock())
    try:
        await monitor.deliver_outbox(now, lease)
        repo.set_delivery.assert_any_await(old.id, "superseded_strategy", expected="pending")
        repo.set_delivery.assert_any_await(new.id, "sent", ANY, "test_message")
        assert monitor.telegram.send.await_count == 1
        assert "sweep_split_cvd" in monitor.telegram.send.call_args.args[0]
    finally:
        await monitor.stop()


@pytest.mark.asyncio
async def test_new_sweep_cooldown_is_separate_from_legacy_and_snapshot_frozen():
    from decimal import Decimal

    data = setup_bars()
    settings = Settings(_env_file=None, hourly_strategy="sweep", hourly_telegram_enabled=True)
    start = datetime.fromisoformat(data[-1]["start"])
    now = datetime.fromisoformat(data[-1]["decision_at"])
    repo = SimpleNamespace(
        bars=AsyncMock(side_effect=[[data[-1]], data]),
        evaluated=AsyncMock(return_value=False),
        entry_sample=AsyncMock(return_value=(Decimal(data[-1]["price"]["close"]), now)),
        cooling_down=AsyncMock(return_value=False),
        save_alert=AsyncMock(),
        mark_evaluated=AsyncMock(),
    )
    monitor = HourlyMonitor(settings, repo)
    try:
        await monitor.process(start, now, True, None)
        assert repo.cooling_down.call_args.kwargs == {"kind": "sweep_split_cvd"}
        record = repo.save_alert.call_args.args[0]
        assert record["delivery_state"] == "pending"
        assert record["snapshot"]["parameters"]["hourly_strategy"] == "sweep"
        assert record["snapshot"]["candidate"]["kind"] == "sweep_split_cvd"
    finally:
        await monitor.stop()
