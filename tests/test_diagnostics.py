from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from btc_monitor.diagnostics import cvd_diagnostics
from btc_monitor.domain import Direction

D = Decimal
START = datetime(2026, 1, 1, 12, tzinfo=UTC)


def flow_rows():
    return [
        SimpleNamespace(
            bucket_start=START + timedelta(minutes=i),
            delta_notional=D("10"),
            cumulative_cvd=D((i + 1) * 10),
            is_complete=True,
        )
        for i in range(111)
    ]


def test_exact_close_boundaries_exclude_next_minute_and_current_baseline():
    rows = flow_rows()
    # Pivot candle closes at minute 85; signal closes at minute 105.
    # Poison all rows beyond signal close; they must NEVER influence the calculation.
    rows[105].delta_notional = D("9999999")
    rows[105].cumulative_cvd = D("9999999")
    divergence, delta, debug = cvd_diagnostics(
        rows, START + timedelta(minutes=80), START + timedelta(minutes=100), Direction.LONG
    )
    assert delta == D("50")
    assert debug["reference_cvd"] == "850"
    assert debug["current_cvd"] == "1050"
    assert debug["normalizer_raw"] == "50"
    assert debug["baseline_count"] == 20
    assert debug["baseline_bars"][-1]["start"] == (START + timedelta(minutes=95)).isoformat()
    assert divergence.strength == D("4")
    assert debug["sum_deltas_between_closes"] == debug["cvd_difference"]
    assert divergence.valid


def test_missing_pivot_minute_is_not_replaced_by_older_cvd():
    rows = flow_rows()
    rows.pop(84)
    divergence, _, debug = cvd_diagnostics(
        rows, START + timedelta(minutes=80), START + timedelta(minutes=100), Direction.LONG
    )
    assert not divergence.valid
    assert debug["reference_cvd"] is None
    assert "pivot_close_minute_missing" in debug["issues"]


def test_gap_and_restart_partial_invalidate_divergence():
    rows = flow_rows()
    rows[90].is_complete = False
    rows.pop(91)
    divergence, _, debug = cvd_diagnostics(
        rows, START + timedelta(minutes=80), START + timedelta(minutes=100), Direction.LONG
    )
    assert not divergence.valid
    assert "missing_minutes" in debug["issues"]
    assert "incomplete_or_legacy_minutes" in debug["issues"]


def test_cvd_corruption_and_epsilon_floor_are_diagnosed():
    rows = flow_rows()
    rows[90].cumulative_cvd += D("100")
    _, _, debug = cvd_diagnostics(
        rows, START + timedelta(minutes=80), START + timedelta(minutes=100), Direction.LONG
    )
    assert "cumulative_cvd_not_equal_to_summed_deltas" in debug["issues"]
    for r in rows:
        r.delta_notional = D(0)
        r.cumulative_cvd = D(0)
    divergence, _, debug = cvd_diagnostics(
        rows, START + timedelta(minutes=80), START + timedelta(minutes=100), Direction.LONG
    )
    assert debug["epsilon_used"]
    assert not divergence.valid


def test_short_direction_and_large_normal_accumulation():
    rows = flow_rows()
    for r in rows:
        r.delta_notional = -r.delta_notional
        r.cumulative_cvd = -r.cumulative_cvd
    divergence, _, debug = cvd_diagnostics(
        rows, START + timedelta(minutes=50), START + timedelta(minutes=100), Direction.SHORT
    )
    assert divergence.valid
    assert divergence.strength == D("10")
    assert not debug["epsilon_used"]


def test_database_rounding_tolerance_not_false_corruption():
    rows = flow_rows()
    rows[90].cumulative_cvd += D("0.00000001")
    divergence, _, debug = cvd_diagnostics(
        rows, START + timedelta(minutes=80), START + timedelta(minutes=100), Direction.LONG
    )
    assert divergence.valid
    assert not debug["continuity_error_minutes"]
