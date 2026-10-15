import copy
import gzip
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from btc_monitor.access import MonitorAccess
from btc_monitor.archive.types import Observation, serializable
from btc_monitor.config import Settings
from btc_monitor.hourly.api import routes
from btc_monitor.hourly.cli import save_export
from btc_monitor.hourly.engine import evaluate
from btc_monitor.hourly.features import build_hour, universe
from btc_monitor.hourly.outcomes import track
from btc_monitor.hourly.service import HourlyMonitor, render_alert

D = Decimal
START = datetime(2026, 10, 11, tzinfo=UTC)


def config(**kwargs):
    return Settings(_env_file=None, telegram_enabled=False, **kwargs)


def bars():
    result = []
    for i in range(25):
        t = START + timedelta(hours=i)
        close = D(100) + D(i) / 5
        result.append(
            serializable(
                {
                    "source": "coinalyze",
                    "start": t,
                    "end": t + timedelta(hours=1),
                    "decision_at": t + timedelta(hours=1, minutes=3),
                    "complete": True,
                    "price": {
                        "open": close - D(".1"),
                        "close": close,
                        "high": close + 1,
                        "low": close - 1,
                    },
                    "expected_feeds": [i.key for i in universe("coinalyze")],
                    "input_ids": [i + 1],
                    "flows": {
                        j.key: {"delta_btc": D(-2), "volume_btc": D(20)}
                        for j in universe("coinalyze")
                    },
                    "aggregated": {
                        "spot": {"delta_btc": D(-6), "volume_btc": D(60)},
                        "linear": {"delta_btc": D("-.2"), "volume_btc": D(100)},
                    },
                    "oi_change_pct": D(".4"),
                    "funding_fraction_mean": D(".00004"),
                    "funding_basis": "unweighted_reported_rate_mean",
                    "derivatives": {},
                    "oi_btc": D(60000),
                    "excluded_feeds": ["okx:spot:BTC-USDT"],
                }
            )
        )
    return result


def observed(inst, dataset, index, payload, row_id, available=None, quality="complete"):
    t = START + timedelta(minutes=index)
    obs = Observation(
        inst.venue,
        inst.market,
        inst.symbol,
        dataset,
        60,
        t,
        t + timedelta(minutes=1),
        t + timedelta(minutes=1, seconds=30),
        "coinalyze_history",
        "test",
        quality,
        payload,
    )
    return {**obs.values(), "id": row_id, "available_at": available or obs.received_at}


def provider_rows():
    result = []
    for inst in universe("coinalyze"):
        for minute in range(60):
            result.append(
                observed(
                    inst,
                    "provider_flow",
                    minute,
                    {
                        "provider": "coinalyze",
                        "provider_symbol": inst.key,
                        "volume_unit": "base",
                        "total_native": "10",
                        "delta_native": "-2",
                        "open": "100",
                        "high": "102",
                        "low": "99",
                        "close": "101",
                    },
                    len(result) + 1,
                )
            )
        if inst.market == "linear":
            for minute, value in [(-1, "100"), (59, "110")]:
                result.append(
                    observed(
                        inst,
                        "provider_oi",
                        minute,
                        {
                            "provider": "coinalyze",
                            "provider_symbol": inst.key,
                            "volume_unit": "base",
                            "reported_oi_native": value,
                            "reported_oi_btc": value,
                        },
                        len(result) + 1,
                    )
                )
            result.append(
                observed(
                    inst,
                    "provider_funding",
                    59,
                    {"provider": "coinalyze", "rate_fraction": "0.00004"},
                    len(result) + 1,
                )
            )
    return result


def test_price_rebound_spot_sell_futures_weak_is_watch():
    result = evaluate(bars(), config())
    assert result["blocked"] is None
    assert [(s["direction"], s["tier"]) for s in result["candidates"]] == [("SHORT", "WATCH")]
    d = result["diagnostics"]
    assert D(d["spot_pressure"]) == D("-.1")
    assert D(d["futures_spot_pressure_ratio"]) < D(".1")
    assert d["spot_normalizer_btc"] == "360"
    assert not d["cumulative_reset_used"]


def test_rejected_high_upgrades_to_confirmation():
    data = bars()
    data[20]["price"]["high"] = "110"
    data[-1]["price"].update(open="110", high="111", low="108", close="109")
    result = evaluate(data, config())
    candidate = result["candidates"][0]
    assert candidate["tier"] == "CONFIRMED"
    assert candidate["kind"] == "breakout_rejection"
    reference = result["diagnostics"]["references"]["SHORT"]
    assert reference["pivot_start"] == data[20]["start"]
    assert reference["pivot_confirmed_at"] == data[21]["end"]
    assert D(candidate["invalidation"]) > 111


def test_lower_high_retest_and_future_pivot_not_used():
    data = bars()
    data[20]["price"]["high"] = "110"
    data[-1]["price"].update(open="109.1", high="109.9", low="108", close="109")
    result = evaluate(data, config())
    assert result["candidates"][0]["kind"] == "failed_retest"
    data[23]["price"]["high"] = "200"
    # Penultimate bar cannot be a reference: its right confirmation is current, not prior.
    assert (
        evaluate(data, config())["diagnostics"]["references"]["SHORT"]["pivot_start"]
        == data[20]["start"]
    )


@pytest.mark.parametrize("fault", ["gap", "cohort", "time", "normalizer", "short"])
def test_bad_context_blocks_instead_of_fake_signal(fault):
    data = bars()
    if fault == "gap":
        data[-2]["complete"] = False
    if fault == "cohort":
        data[-2]["expected_feeds"] = ["bybit:spot:BTCUSDT"]
    if fault == "time":
        data[-2]["start"] = data[-3]["start"]
    if fault == "normalizer":
        for b in data[-6:]:
            b["aggregated"]["spot"]["volume_btc"] = "0"
    if fault == "short":
        data = data[-10:]
    result = evaluate(data, config())
    assert result["blocked"] and not result["candidates"]


def test_single_venue_cannot_create_rolling_watch():
    data = bars()
    for b in data:
        for k, v in b["flows"].items():
            if ":spot:" in k and not k.startswith("bybit:"):
                v["delta_btc"] = "3"
    assert not evaluate(data, config())["candidates"]


def test_old_spot_gap_does_not_poison_valid_price_or_recent_flow():
    data = bars()
    data[3]["complete"] = False
    data[3]["aggregated"]["spot"]["delta_btc"] = None
    data[3]["aggregated"]["spot"]["volume_btc"] = None
    data[2]["price"]["high"] = "110"
    result = evaluate(data, config())
    assert result["blocked"] is None and result["candidates"]
    assert result["diagnostics"]["references"]["SHORT"]["pivot_to_now_delta_btc"] is None


async def test_replay_includes_baseline_and_forces_only_analysis_not_inputs():
    monitor = HourlyMonitor(config(), SimpleNamespace())
    monitor.process = AsyncMock()
    lease = SimpleNamespace(exec_driver_sql=AsyncMock(), commit=AsyncMock())
    try:
        await monitor.rebuild(START, START + timedelta(hours=2), lease)
        assert monitor.process.await_count == 26
        first = monitor.process.call_args_list[0]
        assert first.args[0] == START - timedelta(hours=24)
        assert first.args[2] is False and first.kwargs["force"]
    finally:
        await monitor.stop()


def test_symmetric_bullish_pressure_divergence():
    data = bars()
    for i, b in enumerate(data):
        close = D(110) - D(i) / 5
        b["price"] = {
            "open": str(close + D(".1")),
            "close": str(close),
            "high": str(close + 1),
            "low": str(close - 1),
        }
        b["aggregated"]["spot"]["delta_btc"] = "6"
        for k, v in b["flows"].items():
            if ":spot:" in k:
                v["delta_btc"] = "2"
    assert evaluate(data, config())["candidates"][0]["direction"] == "LONG"


def test_feature_causal_fixed_cohort_correct_funding_oi():
    rows = provider_rows()
    b = build_hour(rows, START, START + timedelta(hours=1, minutes=3))
    assert b["complete"]
    assert b["excluded_feeds"] == ["okx:spot:BTC-USDT"]
    assert b["aggregated"]["spot"]["delta_btc"] == "-360"
    assert D(b["oi_change_pct"]) == 10 and D(b["oi_btc"]) == 330
    assert b["funding_fraction_mean"] == "0.00004"
    assert len(b["input_ids"]) == 369
    revised = copy.deepcopy(rows[0])
    revised["id"] = 99999
    revised["available_at"] = START + timedelta(hours=2)
    revised["payload"]["delta_native"] = "999"
    later = build_hour(rows + [revised], START, START + timedelta(hours=1, minutes=3))
    assert b == later


def test_missing_spot_minute_or_unit_does_not_shrink_cohort():
    rows = provider_rows()[1:]
    b = build_hour(rows, START, START + timedelta(hours=1, minutes=3))
    assert not b["complete"] and b["aggregated"]["spot"]["delta_btc"] is None
    rows = provider_rows()
    rows[0]["payload"]["volume_unit"] = "unknown"
    assert not build_hour(rows, START, START + timedelta(hours=1, minutes=3))["complete"]


def test_stale_derivatives_remain_missing_not_zero():
    rows = [r for r in provider_rows() if r["dataset"] == "provider_flow"]
    b = build_hour(rows, START, START + timedelta(hours=1, minutes=3))
    assert b["complete"] and b["oi_change_pct"] is None and b["funding_fraction_mean"] is None


def prior_unit_metadata():
    at = START - timedelta(minutes=1)
    payload = {
        "provider": "coinalyze",
        "selected": [
            {"feed": i.key, "provider_symbol": i.key, "volume_unit": "base"}
            for i in universe("coinalyze")
            if i.market == "spot"
        ],
        "calibration": {
            i.key: {
                "verified": True,
                "matched_minutes": 5,
                "max_relative_error": "0.00000001",
                "end": (START - timedelta(hours=25)).isoformat(),
            }
            for i in universe("coinalyze")
            if i.market == "spot"
        },
    }
    row = Observation(
        "coinalyze",
        "metadata",
        "BTC",
        "provider_metadata",
        0,
        at,
        at,
        at,
        "coinalyze_history",
        "test",
        "complete",
        payload,
    ).values()
    return {**row, "id": 99000, "available_at": at}


async def test_prior_evidence_repairs_review_without_changing_stored_decision_or_raw():
    from btc_monitor.hourly.review import review_bars

    rows = provider_rows()
    for r in rows:
        if r["market"] == "spot":
            r["payload"]["volume_unit"] = "unknown"
    raw_before = copy.deepcopy(rows)
    decision = START + timedelta(hours=1, minutes=3)
    original = build_hour(rows, START, decision)
    assert not original["complete"]
    repo = SimpleNamespace(
        bars=AsyncMock(return_value=[original]),
        observations=AsyncMock(return_value=rows),
        metadata_history=AsyncMock(return_value=[prior_unit_metadata()]),
    )
    repaired = (await review_bars(repo, "coinalyze", START, decision))[0]
    assert repaired["complete"] and len(repaired["unit_recovery"]) == 180
    assert repaired["aggregated"]["spot"]["delta_btc"] == "-360"
    assert repaired["review_recovery"]["original_decision_complete"] is False
    assert not original["complete"] and "review_recovery" not in original
    assert rows == raw_before and 99000 in repaired["input_ids"]


def test_unit_evidence_cannot_fill_a_missing_minute():
    rows = provider_rows()[1:]
    for r in rows:
        if r["market"] == "spot":
            r["payload"]["volume_unit"] = "unknown"
    bar = build_hour(rows + [prior_unit_metadata()], START, START + timedelta(hours=1, minutes=3))
    assert not bar["complete"]
    first = next(i for i in universe("coinalyze") if i.market == "spot")
    assert "missing_minutes" in bar["flows"][first.key]["quality_reasons"]


def price_rows(count=60, missing=None):
    inst = next(i for i in universe("direct") if i.key == "bybit:linear:BTCUSDT")
    rows = []
    for minute in range(count):
        if minute == missing:
            continue
        r = observed(
            inst,
            "flow",
            minute,
            {"open": "100", "high": "102", "low": "98", "close": "101"},
            minute + 1,
        )
        r["source_type"] = "live"
        rows.append(r)
    return rows


def alert():
    return SimpleNamespace(
        id="test",
        generated_at=START,
        entry=D(100),
        invalidation=D(98),
        direction="LONG",
        snapshot={"entry_basis": "fresh_bybit_ticker_at_generation"},
    )


def test_outcomes_ambiguous_same_minute_and_actual_generation_boundary():
    result = track(alert(), price_rows(), START + timedelta(hours=1, minutes=3))
    w = result["windows"]["1"]
    assert w["verdict"] == "direction_supported" and w["coverage"] == "complete_minute_interior"
    assert w["touch_order"] == "ambiguous_same_minute"
    a = alert()
    a.generated_at = START + timedelta(seconds=30)
    result = track(a, price_rows(), START + timedelta(hours=1, minutes=3))
    assert 1 not in result["windows"]["1"]["input_ids"]
    assert result["windows"]["1"]["verdict"] == "direction_supported"


def test_outcome_gap_is_not_a_win_and_future_data_ignored():
    rows = price_rows(missing=20)
    result = track(alert(), rows, START + timedelta(hours=1, minutes=3))
    assert result["windows"]["1"]["verdict"] == "insufficient_data"
    future = copy.deepcopy(rows[0])
    future["event_time"] = START + timedelta(days=2)
    future["end_time"] = future["event_time"] + timedelta(minutes=1)
    future["payload"]["high"] = "99999"
    assert result == track(alert(), rows + [future], START + timedelta(hours=1, minutes=3))


async def test_durable_outbox_uncertainty_and_no_automatic_resend():
    repo = SimpleNamespace(
        pending_deliveries=AsyncMock(),
        set_delivery=AsyncMock(return_value=True),
        entry_sample=AsyncMock(return_value=(D(100), START)),
    )
    a = alert()
    a.tier = "WATCH"
    a.source = "coinalyze"
    a.snapshot.update(
        candidate=evaluate(bars(), config())["candidates"][0],
        diagnostics=evaluate(bars(), config())["diagnostics"],
        parameters={"flow_hours": 6},
    )
    repo.pending_deliveries.return_value = [a]
    settings = config(hourly_telegram_enabled=True)
    monitor = HourlyMonitor(settings, repo)
    monitor.telegram.send = AsyncMock(side_effect=RuntimeError("safe_error"))
    lease = SimpleNamespace(exec_driver_sql=AsyncMock(), commit=AsyncMock())
    try:
        await monitor.deliver_outbox(START, lease)
        assert repo.set_delivery.call_args.args[1] == "uncertain"
        repo.pending_deliveries.return_value = []
        await monitor.deliver_outbox(START, lease)
        assert monitor.telegram.send.await_count == 1
        assert "normalizer" not in render_alert(a)
    finally:
        await monitor.stop()


async def test_retrospective_job_does_not_send_and_stale_delivery_is_suppressed():
    a = alert()
    repo = SimpleNamespace(pending_deliveries=AsyncMock(return_value=[a]), set_delivery=AsyncMock())
    monitor = HourlyMonitor(config(hourly_telegram_enabled=True), repo)
    monitor.telegram.send = AsyncMock()
    try:
        await monitor.deliver_outbox(START + timedelta(hours=1), None)
        assert repo.set_delivery.call_args.args[1] == "stale_delivery"
        monitor.telegram.send.assert_not_awaited()
    finally:
        await monitor.stop()


@pytest.mark.parametrize(
    "price,state", [(None, "missing_fresh_delivery"), (D(97), "invalidated_before_delivery")]
)
async def test_outbox_rechecks_fresh_price_and_invalidation_before_sending(price, state):
    repo = SimpleNamespace(
        pending_deliveries=AsyncMock(return_value=[alert()]),
        set_delivery=AsyncMock(),
        entry_sample=AsyncMock(return_value=(price, START if price is not None else None)),
    )
    monitor = HourlyMonitor(config(hourly_telegram_enabled=True), repo)
    monitor.telegram.send = AsyncMock()
    try:
        await monitor.deliver_outbox(START, None)
        assert repo.set_delivery.call_args.args[1] == state
        monitor.telegram.send.assert_not_awaited()
    finally:
        await monitor.stop()


@pytest.mark.parametrize(
    "peer,password,auth,status",
    [
        ("127.0.0.1", None, None, 200),
        ("8.8.8.8", None, None, 403),
        ("8.8.8.8", "safe-password", None, 401),
        ("8.8.8.8", "safe-password", ("monitor", "wrong"), 401),
        ("8.8.8.8", "safe-password", ("monitor", "safe-password"), 200),
    ],
)
async def test_interface_requires_auth_or_real_loopback_not_forwarded_headers(
    peer, password, auth, status
):
    app = FastAPI()
    app.add_middleware(MonitorAccess, password=SecretStr(password) if password else None)

    @app.get("/data")
    async def data():
        return {"safe": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 1234)), base_url="http://test"
    ) as client:
        r = await client.get("/data", auth=auth, headers={"x-forwarded-for": "127.0.0.1"})
        assert r.status_code == status
        if status == 200:
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_api_replay_csrf_bounds_and_static_assets():
    app = FastAPI()
    m = SimpleNamespace(
        settings=config(),
        request_replay=AsyncMock(return_value=True),
        health=lambda: {"enabled": False},
    )
    app.include_router(routes(m, SimpleNamespace()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/dashboard")).status_code == 200
        assert (await c.get("/dashboard/assets/app.js")).status_code == 200
        assert (await c.get("/dashboard/assets/private.env")).status_code == 404
        assert (await c.post("/hourly/replay?hours=169", json={})).status_code == 422
        assert (
            await c.post("/hourly/replay", json={}, headers={"Origin": "https://evil.test"})
        ).status_code == 403
        assert (await c.post("/hourly/replay", content="{}")).status_code == 415
        assert (await c.post("/hourly/replay", json={})).status_code == 202
        m.request_replay.assert_awaited_once()


async def test_signal_detail_and_raw_export_support_uuid_decimal_and_time():
    identifier = uuid4()
    row = SimpleNamespace(
        id=identifier,
        source="coinalyze",
        bar_start=START,
        generated_at=START,
        direction="SHORT",
        tier="WATCH",
        origin="retrospective",
        entry=D(100),
        invalidation=D(110),
        outcomes=None,
        tracking_complete=False,
        delivery_state="retrospective",
        delivery_at=None,
        snapshot={"candidate": {"kind": "cvd_divergence", "reasons": ["test"]}, "input_ids": []},
    )
    repo = SimpleNamespace(alert=AsyncMock(return_value=row), raw_inputs=AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(routes(SimpleNamespace(), repo))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.get(f"/hourly/signals/{identifier}")
        assert response.status_code == 200 and response.json()["id"] == str(identifier)
        response = await c.get(f"/hourly/signals/{identifier}/export")
        assert response.status_code == 200 and response.json()["signal"]["entry"] == "100"


def test_export_never_overwrites_existing_file(tmp_path):
    target = tmp_path / "review.json"
    save_export(target, {"schema": "btc-hourly-review-v1", "signal_id": uuid4()})
    with pytest.raises(FileExistsError):
        save_export(target, {})
    invalid = tmp_path / "invalid.json"
    with pytest.raises(TypeError):
        save_export(invalid, {"unsupported": object()})
    assert not invalid.exists()


def test_compressed_review_export_is_lossless(tmp_path):
    payload = {"schema": "btc-hourly-review-v1", "signals": [{"id": uuid4(), "reason": "量价背离"}]}
    path = tmp_path / "review.json.gz"
    save_export(path, payload)
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        result = json.load(stream)
    assert result["signals"][0]["id"] == str(payload["signals"][0]["id"])
    assert result["signals"][0]["reason"] == "量价背离"
