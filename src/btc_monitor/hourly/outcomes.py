from datetime import timedelta
from decimal import Decimal

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.scoring import confidence, review_score

HORIZONS = (1, 4, 12, 24)
VERSION = "h1-path-v2"
D = Decimal
MINUTE = timedelta(minutes=1)


def track(alert, rows, now):
    start, entry, stop = alert.generated_at, D(alert.entry), D(alert.invalidation)
    sign = 1 if alert.direction == "LONG" else -1
    risk = abs(entry - stop)
    gaps = [
        r
        for r in rows
        if r["dataset"] == "quality"
        and r["available_at"] <= now
        and (r["venue"], r["market"], r["instrument"]) == ("bybit", "linear", "BTCUSDT")
    ]
    first_minute = start.replace(second=0, microsecond=0)
    if first_minute < start:
        first_minute += MINUTE
    choices, rejected = {}, {}
    boundary = None
    # REST closed candles are independent price evidence, not repaired trade/CVD flow.
    for r in sorted(
        rows, key=lambda r: (r["source_type"] == "rest_history", r["available_at"], r["id"])
    ):
        if (
            (r["venue"], r["market"], r["instrument"]) != ("bybit", "linear", "BTCUSDT")
            or r["dataset"] not in {"flow", "candle"}
            or r["source_type"] not in {"live", "rest_history"}
            or r["interval_seconds"] != 60
            or r["event_time"] < start.replace(second=0, microsecond=0)
            or r["event_time"].second
            or r["event_time"].microsecond
            or r["end_time"] != r["event_time"] + MINUTE
            or r["end_time"] > now
            or r["available_at"] > now
        ):
            continue
        reason = None
        try:
            o, h, low_, c = (D(str(r["payload"][k])) for k in ("open", "high", "low", "close"))
            valid = (
                all(v.is_finite() and v > 0 for v in (o, h, low_, c))
                and low_ <= min(o, c) <= max(o, c) <= h
            )
        except (KeyError, ValueError, ArithmeticError, TypeError):
            valid = False
        if r["quality"] != "complete":
            reason = "record_not_complete"
        elif not valid:
            reason = "invalid_ohlc"
        elif r["source_type"] == "live" and any(
            g["event_time"] < r["end_time"] and g["end_time"] > r["event_time"] for g in gaps
        ):
            reason = "live_quality_gap"
        if reason:
            rejected[r["event_time"]] = reason
        elif r["event_time"] < first_minute:
            boundary = r
        else:
            choices[r["event_time"]] = r
    target = entry + sign * risk
    boundary_clear = first_minute == start or bool(
        boundary
        and D(boundary["payload"]["low"]) > min(stop, target)
        and D(boundary["payload"]["high"]) < max(stop, target)
    )
    windows = {}
    for hours in HORIZONS:
        deadline = start + timedelta(hours=hours)
        end = min(now, deadline)
        full_end = end.replace(second=0, microsecond=0)
        expected = max(0, int((full_end - first_minute).total_seconds() // 60))
        expected_times = [first_minute + i * MINUTE for i in range(expected)]
        missing = [t for t in expected_times if t not in choices]
        bars = [choices[t] for t in expected_times if t in choices]
        complete = expected > 0 and not missing
        matured = now >= deadline + timedelta(minutes=2)
        last = bars[-1] if bars else None
        # Do not call a stale earlier close the horizon endpoint.
        endpoint_exact = bool(last and last["end_time"] == full_end)
        signed = sign * (D(last["payload"]["close"]) - entry) / entry * 100 if last else None
        mfe = max(
            [D(0)]
            + [
                sign * (D(r["payload"]["high"] if sign > 0 else r["payload"]["low"]) - entry)
                for r in bars
            ]
        )
        mae = max(
            [D(0)]
            + [
                -sign * (D(r["payload"]["low"] if sign > 0 else r["payload"]["high"]) - entry)
                for r in bars
            ]
        )
        hits = {}
        for r in bars:
            high, low = D(r["payload"]["high"]), D(r["payload"]["low"])
            for name, level in {
                "invalidation": stop,
                "1R": entry + sign * risk,
                "2R": entry + sign * risk * 2,
            }.items():
                touched = (
                    (low <= level if sign > 0 else high >= level)
                    if name == "invalidation"
                    else (high >= level if sign > 0 else low <= level)
                )
                if touched and name not in hits:
                    hits[name] = {"from": r["event_time"], "to": r["end_time"], "id": r["id"]}
        a, b = hits.get("1R"), hits.get("invalidation")
        ordering = (
            "ambiguous_same_minute"
            if a and b and a["id"] == b["id"]
            else "target_first"
            if a and b and a["to"] <= b["from"]
            else "invalidation_first"
            if a and b
            else "target_only"
            if a
            else "invalidation_only"
            if b
            else "neither"
        )
        first_hit = min(
            (h["to"] for name, h in hits.items() if name in {"1R", "invalidation"}), default=None
        )
        # Gaps AFTER the first known event cannot change which event happened first.
        prefix_complete = (
            boundary_clear and first_hit is not None and not any(t < first_hit for t in missing)
        )
        order_proven = prefix_complete or (complete and boundary_clear)
        touch_order = (
            ordering
            if order_proven
            else "unresolved_missing_prefix"
            if missing
            else "unresolved_partial_start_minute"
        )
        windows[str(hours)] = {
            "deadline": deadline,
            "status": "complete" if matured else "pending",
            "coverage": "complete_minute_interior" if complete else "incomplete",
            "expected_minutes": expected,
            "observed_minutes": len(bars),
            "missing_minutes": [
                {"start": t, "reason": rejected.get(t, "no_eligible_record")} for t in missing
            ],
            "data_quality_score": round(100 * len(bars) / expected, 2) if expected else None,
            "endpoint_exact": endpoint_exact,
            "endpoint_at": last["end_time"] if last else None,
            "endpoint_boundary_seconds": (end - full_end).total_seconds(),
            "signed_return_pct": signed if endpoint_exact else None,
            "exit_price": D(last["payload"]["close"]) if last else None,
            "verdict": "pending"
            if not matured
            else "insufficient_data"
            if not endpoint_exact
            else "direction_supported"
            if signed > D("0.05")
            else "direction_opposed"
            if signed < D("-0.05")
            else "flat",
            "mfe_usdt_lower_bound": mfe if bars else None,
            "mae_usdt_lower_bound": mae if bars else None,
            "mfe_r_lower_bound": mfe / risk if risk and bars else None,
            "first_touches": hits,
            "observed_touch_order": ordering,
            "touch_order": touch_order,
            "touch_order_proven": order_proven,
            "partial_start_minute": first_minute != start,
            "start_boundary_clear": boundary_clear,
            "boundary_input_id": boundary["id"] if boundary else None,
            "scores": review_score(
                signed, risk, entry, touch_order, endpoint_exact, matured, complete
            ),
            "input_ids": [r["id"] for r in bars],
            "price_repair_ids": [r["id"] for r in bars if r["source_type"] == "rest_history"],
            "quality_gap_ids": [
                r["id"] for r in gaps if r["event_time"] < end and r["end_time"] > start
            ],
        }
    return serializable(
        {
            "version": VERSION,
            "updated_at": now,
            "start_basis": "actual_generated_at",
            "entry_basis": alert.snapshot["entry_basis"],
            "confidence": alert.snapshot.get("confidence") or confidence(alert.snapshot),
            "confidence_origin": "at_generation"
            if "confidence" in alert.snapshot
            else "historical_snapshot_rescore",
            "windows": windows,
            "notice": (
                "方向验证不是实际盈利；方向分与结构分独立，分钟边界/分钟内路径未知，"
                "极值是下界；补采仅修复复盘，不改信号。"
            ),
        }
    )
