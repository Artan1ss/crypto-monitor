"""Event-time CVD calculations and lossless JSON diagnostics (no strategy tuning)."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from statistics import median
from uuid import UUID

from btc_monitor.domain import ZERO, Direction, Divergence
from btc_monitor.indicators import EPSILON


def json_value(value):
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_value(v) for v in value]
    return value


def flow_row_snapshot(row) -> dict:
    return json_value(
        {
            k: getattr(row, k, None)
            for k in (
                "market",
                "symbol",
                "bucket_start",
                "buy_notional",
                "sell_notional",
                "delta_notional",
                "cumulative_cvd",
                "trade_count",
                "is_complete",
                "created_at",
            )
        }
    )


def cvd_diagnostics(rows, pivot_start, candle_start, direction, minimum_history=12):
    """Use exact minute closes; exclude the current candle from the 20-bar baseline.

    Pivot CVD is the cumulative close of the pivot's 5m candle, NOT the CVD
    at the intrabar extreme. Missing/incomplete captured trades invalidate the input.
    """
    end = candle_start + timedelta(minutes=5)
    pivot_end = pivot_start + timedelta(minutes=5)
    by_time = {r.bucket_start: r for r in rows if r.bucket_start < end}
    reference = by_time.get(pivot_end - timedelta(minutes=1))
    current = by_time.get(end - timedelta(minutes=1))
    signal_rows = [by_time.get(candle_start + timedelta(minutes=i)) for i in range(5)]
    histories = []
    invalid_baseline = []
    for offset in range(20, 0, -1):
        start = candle_start - timedelta(minutes=offset * 5)
        group = [by_time.get(start + timedelta(minutes=i)) for i in range(5)]
        present = all(r is not None for r in group)
        complete = present and all(r.is_complete is True for r in group)
        item = {
            "start": start,
            "end": start + timedelta(minutes=5),
            "delta": sum((r.delta_notional for r in group if r is not None), ZERO),
            "present": present,
            "complete": complete,
        }
        if complete:
            histories.append(item)
        else:
            invalid_baseline.append(item)
    deltas = [item["delta"] for item in histories]
    normalizer = Decimal(str(median([abs(d) for d in deltas]))) if deltas else ZERO
    denominator = max(normalizer, EPSILON)
    difference = (
        current.cumulative_cvd - reference.cumulative_cvd
        if current is not None and reference is not None
        else ZERO
    )
    favorable = difference if direction == Direction.LONG else -difference
    strength = favorable / denominator if current is not None and reference is not None else ZERO
    missing, incomplete, continuity_errors = [], [], []
    t = pivot_start
    previous = None
    while t < end:
        row = by_time.get(t)
        if row is None:
            missing.append(t)
        elif row.is_complete is not True:
            incomplete.append(t)
        if row is not None and previous is not None:
            if abs(row.cumulative_cvd - previous.cumulative_cvd - row.delta_notional) > EPSILON * 2:
                continuity_errors.append(t)
        previous = row
        t += timedelta(minutes=1)
    between = [r for t, r in sorted(by_time.items()) if pivot_end <= t < end]
    summed_difference = sum((r.delta_notional for r in between), ZERO)
    issues = []
    if reference is None:
        issues.append("pivot_close_minute_missing")
    if current is None:
        issues.append("signal_close_minute_missing")
    if missing:
        issues.append("missing_minutes")
    if incomplete:
        issues.append("incomplete_or_legacy_minutes")
    if continuity_errors or (
        reference
        and current
        and abs(summed_difference - difference) > EPSILON * max(2, len(between))
    ):
        issues.append("cumulative_cvd_not_equal_to_summed_deltas")
    if len(histories) < minimum_history:
        issues.append("insufficient_complete_baseline_bars")
    if normalizer <= EPSILON:
        issues.append("normalizer_zero_or_epsilon_floor")
    delta = sum((r.delta_notional for r in signal_rows if r is not None), ZERO)
    absolute_deltas = [abs(d) for d in deltas]
    mean_absolute_delta = sum(absolute_deltas, ZERO) / len(deltas) if deltas else ZERO
    comparison_bars = Decimal(len(between)) / Decimal(5)
    payload = json_value(
        {
            "formula": (
                "directional(current_close_cvd - pivot_close_cvd) / median(abs(previous_5m_delta))"
            ),
            "unit": "USDT_notional",
            "pivot_sampling": "5m_candle_close_not_intrabar_extreme",
            "pivot_start": pivot_start,
            "pivot_end_exclusive": pivot_end,
            "reference_minute": reference.bucket_start if reference else None,
            "reference_cvd": reference.cumulative_cvd if reference else None,
            "signal_start": candle_start,
            "signal_end_exclusive": end,
            "current_minute": current.bucket_start if current else None,
            "current_cvd": current.cumulative_cvd if current else None,
            "cvd_difference": difference,
            "directional_difference": favorable,
            "sum_deltas_between_closes": summed_difference,
            "signal_delta_5m": delta,
            "normalizer_raw": normalizer,
            "normalizer_effective": denominator,
            "baseline_abs_delta_min": min(absolute_deltas) if deltas else None,
            "baseline_abs_delta_max": max(absolute_deltas) if deltas else None,
            "baseline_abs_delta_mean": mean_absolute_delta,
            "normalizer_to_mean_ratio": normalizer / mean_absolute_delta
            if mean_absolute_delta > ZERO
            else None,
            "signal_delta_in_normalizer_units": delta / denominator,
            "epsilon": EPSILON,
            "epsilon_used": normalizer <= EPSILON,
            "strength": strength,
            "divergence_threshold": "0.5",
            "valid": not issues and strength >= Decimal("0.5"),
            "data_valid": not issues,
            "issues": issues,
            "baseline_bars": histories,
            "excluded_baseline_bars": invalid_baseline,
            "baseline_count": len(histories),
            "missing_minutes": missing,
            "incomplete_minutes": incomplete,
            "continuity_error_minutes": continuity_errors,
            "ignored_rows_at_or_after_signal_end": [
                r.bucket_start for r in rows if r.bucket_start >= end
            ],
            "difference_in_baseline_units": strength,
            "comparison_minutes": len(between),
            "comparison_five_minute_bars": comparison_bars,
            "average_difference_per_5m_in_normalizer_units": strength / comparison_bars
            if comparison_bars > ZERO
            else None,
            "mean_delta_between_closes": summed_difference / len(between) if between else ZERO,
        }
    )
    return Divergence(payload["valid"], strength), delta, payload
