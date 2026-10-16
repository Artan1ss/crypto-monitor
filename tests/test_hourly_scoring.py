"""Path evidence, not hindsight wins: both directions and missing prefixes."""

import copy
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_hourly import START, alert, bars, config, price_rows

from btc_monitor.hourly.engine import evaluate, parameters
from btc_monitor.hourly.outcomes import track
from btc_monitor.hourly.scoring import confidence
from btc_monitor.hourly.service import HourlyMonitor


def path(missing=None):
    rows = price_rows(missing=missing)
    for r in rows:
        r["payload"].update(open="100", high="101", low="99", close="100")
        m = int((r["event_time"] - START).total_seconds() / 60)
        if m == 10:
            r["payload"]["high"] = "103"
        if m == 30:
            r["payload"]["low"] = "97"
    return rows


@pytest.mark.parametrize(
    "missing,order", [(5, "unresolved_missing_prefix"), (20, "target_first"), (45, "target_first")]
)
def test_only_gaps_before_first_touch_prevent_order_proof(missing, order):
    w = track(alert(), path(missing), START + timedelta(hours=1, minutes=3))["windows"]["1"]
    assert w["touch_order"] == order
    assert w["observed_touch_order"] == "target_first"
    assert w["observed_minutes"] == 59 and len(w["missing_minutes"]) == 1
    assert w["scores"]["structure_score"] == (None if missing == 5 else 100)
    assert w["scores"]["direction_score"] == "50.0"


def test_short_direction_success_does_not_erase_prior_invalidation():
    a = alert()
    a.direction, a.invalidation = "SHORT", D(102)
    w = track(a, path(), START + timedelta(hours=1, minutes=3))["windows"]["1"]
    assert w["touch_order"] == "invalidation_first"
    assert w["scores"]["structure_score"] == 0


def test_missing_endpoint_does_not_use_stale_close_or_score():
    w = track(alert(), path(59), START + timedelta(hours=1, minutes=3))["windows"]["1"]
    assert not w["endpoint_exact"] and w["verdict"] == "insufficient_data"
    assert w["signed_return_pct"] is None and w["scores"]["direction_score"] is None
    assert w["touch_order"] == "target_first"


def test_rest_price_can_repair_live_gap_but_not_future_or_wrong_venue():
    rows = path(5)
    repair = path()[5]
    repair.update(id=999, source_type="rest_history", dataset="candle")
    gap = copy.deepcopy(repair)
    gap.update(id=998, source_type="live", dataset="quality", quality="gap")
    rows.append(gap)
    bad = copy.deepcopy(repair)
    bad.update(id=997, venue="binance")
    now = START + timedelta(hours=1, minutes=3)
    assert track(alert(), rows + [bad], now)["windows"]["1"]["coverage"] == "incomplete"
    repair["available_at"] = now + timedelta(minutes=1)
    assert track(alert(), rows + [repair], now)["windows"]["1"]["coverage"] == "incomplete"
    w = track(alert(), rows + [repair], now + timedelta(minutes=2))["windows"]["1"]
    assert w["coverage"] == "complete_minute_interior" and w["observed_minutes"] == 60
    assert w["price_repair_ids"] == [999] and w["touch_order"] == "target_first"
    assert rows[5]["source_type"] == "live"  # inputs stay immutable


def test_incomplete_live_minute_cannot_replace_valid_rest_and_invalid_ohlc_rejected():
    rows = path()
    good = copy.deepcopy(rows[5])
    good.update(id=998, source_type="rest_history", dataset="candle")
    rows[5]["quality"] = "partial"
    rows[6]["payload"]["close"] = "NaN"
    w = track(alert(), rows + [good], START + timedelta(hours=1, minutes=3))["windows"]["1"]
    assert w["observed_minutes"] == 59 and w["price_repair_ids"] == [998]
    assert w["missing_minutes"][0]["reason"] == "invalid_ohlc"


def test_partial_start_requires_boundary_clearance_not_ignoring_first_seconds():
    a = alert()
    a.generated_at += timedelta(seconds=30)
    rows = path()
    w = track(a, rows, START + timedelta(hours=1, minutes=3))["windows"]["1"]
    assert w["expected_minutes"] == 59 and w["start_boundary_clear"]
    assert w["touch_order"] == "target_first" and w["boundary_input_id"] == rows[0]["id"]
    rows[0]["payload"]["low"] = "97"
    w = track(a, rows, START + timedelta(hours=1, minutes=3))["windows"]["1"]
    assert w["touch_order"] == "unresolved_partial_start_minute"
    assert w["scores"]["structure_score"] is None


def test_same_minute_ambiguity_no_structure_score_and_no_fixed_off_by_one():
    result = track(alert(), price_rows(1440), START + timedelta(days=1, minutes=3))
    for hours, w in result["windows"].items():
        assert w["observed_minutes"] == w["expected_minutes"] == int(hours) * 60
        assert w["scores"]["structure_score"] is None
        assert w["touch_order"] == "ambiguous_same_minute"


def test_confidence_frozen_uses_only_original_snapshot_and_bounds():
    data = bars()
    data[-1]["price"].update(high="107.5", close="106", open="107")
    analysis = evaluate(data, config())
    snapshot = {
        "diagnostics": analysis["diagnostics"],
        "candidate": analysis["candidates"][0],
        "parameters": parameters(config()),
        "entry_basis": "test",
    }
    original = copy.deepcopy(snapshot)
    score = confidence(snapshot)
    assert snapshot == original and 0 <= score["score"] <= 100
    assert sum(p["max"] for p in score["components"]) == 100
    assert sum(p["points"] for p in score["components"]) == score["score"]
    a = alert()
    snapshot["confidence"] = score
    a.snapshot = snapshot
    rows = path()
    before = track(a, rows, START + timedelta(hours=1, minutes=3))
    rows[-1]["payload"].update(high="200", close="200")
    after = track(a, rows, START + timedelta(hours=1, minutes=3))
    assert before["confidence"] == after["confidence"] == score
    assert before["confidence_origin"] == "at_generation"


async def test_legacy_completed_records_recomputed_without_telegram():
    a = alert()
    a.outcomes = {"windows": {"24": {"coverage": "incomplete"}}}
    repo = SimpleNamespace(
        outcome_candidates=AsyncMock(return_value=[a]),
        observations=AsyncMock(return_value=path()),
        save_outcomes=AsyncMock(),
    )
    monitor = HourlyMonitor(config(), repo)
    monitor.telegram.send = AsyncMock()
    try:
        await monitor.update_outcomes(START + timedelta(days=1, minutes=3), repair=False)
        assert repo.save_outcomes.call_args.args[1]["version"] == "h1-path-v2"
        assert repo.save_outcomes.call_args.args[2] is True
        assert repo.observations.call_args.kwargs["price_only"]
        monitor.telegram.send.assert_not_awaited()
    finally:
        await monitor.stop()


@pytest.mark.parametrize("blocked", [False, True])
async def test_bounded_auto_repair_is_audited_and_never_sends(monkeypatch, blocked):
    from btc_monitor.archive import exchanges, history

    a = alert()
    a.outcomes = None
    repo = SimpleNamespace(
        db=None,
        outcome_candidates=AsyncMock(return_value=[a]),
        observations=AsyncMock(side_effect=[path(5), path()] if not blocked else [path(5)]),
        save_outcomes=AsyncMock(),
    )
    rest = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(exchanges, "PublicRest", lambda _: rest)
    response = httpx.Response(
        403, request=httpx.Request("GET", "https://api.bybit.com/v5/market/kline")
    )
    fetch = AsyncMock(
        side_effect=httpx.HTTPStatusError("not logged", request=response.request, response=response)
        if blocked
        else None,
        return_value={"inserted": 1},
    )
    monkeypatch.setattr(history.HistoryLoader, "prices_and_flow", fetch)
    monitor = HourlyMonitor(config(), repo)
    monitor.telegram.send = AsyncMock()
    try:
        now = START + timedelta(hours=1, minutes=3)
        await monitor.update_outcomes(now)
        result = repo.save_outcomes.call_args.args[1]
        assert fetch.await_count == 1 and rest.close.await_count == 1
        assert fetch.call_args.args[-1] - fetch.call_args.args[-2] <= timedelta(minutes=200)
        if blocked:
            assert result["repair"]["state"] == "failed" and result["repair"]["http_status"] == 403
            assert result["windows"]["1"]["touch_order"] == "unresolved_missing_prefix"
        else:
            assert result["windows"]["1"]["touch_order"] == "target_first"
        # Persisted per-signal cooldown survives a monitor restart.
        a.outcomes = result
        repo.observations.side_effect = None
        repo.observations.return_value = path(5)
        monitor.last_price_repair = None
        await monitor.update_outcomes(now + timedelta(minutes=1))
        assert fetch.await_count == 1
        monitor.telegram.send.assert_not_awaited()
    finally:
        await monitor.stop()
