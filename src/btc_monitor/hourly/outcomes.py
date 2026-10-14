from datetime import timedelta
from decimal import Decimal

from btc_monitor.archive.types import serializable

HORIZONS = (1, 4, 12, 24)
D = Decimal


def track(alert, rows, now):
    start, entry, stop = alert.generated_at, D(alert.entry), D(alert.invalidation)
    sign = 1 if alert.direction == "LONG" else -1
    risk = abs(entry - stop)
    choices = {}
    gaps = [
        r
        for r in rows
        if r["dataset"] == "quality"
        and r["available_at"] <= now
        and (r["venue"], r["market"], r["instrument"]) == ("bybit", "linear", "BTCUSDT")
    ]
    for r in sorted(rows, key=lambda r: (r["available_at"], r["id"])):
        if (
            r["dataset"] in {"flow", "candle"}
            and r["quality"] == "complete"
            and (r["venue"], r["market"], r["instrument"]) == ("bybit", "linear", "BTCUSDT")
            and r["interval_seconds"] == 60
            and r["event_time"] >= start
            and r["end_time"] == r["event_time"] + timedelta(minutes=1)
            and r["end_time"] <= now
            and r["available_at"] <= now
            and r["payload"].get("close") is not None
            and not any(
                g["event_time"] < r["end_time"] and g["end_time"] > r["event_time"] for g in gaps
            )
        ):
            choices[r["event_time"]] = r
    windows = {}
    first_minute = start.replace(second=0, microsecond=0)
    if first_minute < start:
        first_minute += timedelta(minutes=1)
    for hours in HORIZONS:
        deadline = start + timedelta(hours=hours)
        end = min(now, deadline)
        full_end = end.replace(second=0, microsecond=0)
        bars = sorted(
            [r for r in choices.values() if r["end_time"] <= end], key=lambda r: r["event_time"]
        )
        expected = max(0, int((full_end - first_minute).total_seconds() // 60))
        complete = bool(bars) and len(bars) == expected
        last = bars[-1] if bars else None
        signed = sign * (D(last["payload"]["close"]) - entry) / entry * 100 if last else None
        fresh = last and (end - last["end_time"]).total_seconds() <= 120
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
        windows[str(hours)] = {
            "deadline": deadline,
            "status": "complete" if now >= deadline + timedelta(minutes=2) else "pending",
            "coverage": "complete_minute_interior" if complete else "incomplete",
            "expected_minutes": expected,
            "observed_minutes": len(bars),
            "signed_return_pct": signed,
            "exit_price": D(last["payload"]["close"]) if last else None,
            "verdict": "pending"
            if now < deadline + timedelta(minutes=2)
            else "insufficient_data"
            if not complete or not fresh
            else "direction_supported"
            if signed > D("0.05")
            else "direction_opposed"
            if signed < D("-0.05")
            else "flat",
            "mfe_usdt_lower_bound": mfe if bars else None,
            "mae_usdt_lower_bound": mae if bars else None,
            "mfe_r_lower_bound": mfe / risk if risk and bars else None,
            "first_touches": hits,
            "touch_order": ordering if complete else "unknown_due_to_gaps",
            "input_ids": [r["id"] for r in bars],
            "quality_gap_ids": [
                r["id"] for r in gaps if r["event_time"] < end and r["end_time"] > start
            ],
        }
    return serializable(
        {
            "updated_at": now,
            "start_basis": "actual_generated_at",
            "entry_basis": alert.snapshot["entry_basis"],
            "windows": windows,
            "notice": (
                "方向验证不是实际盈利；分钟边界/分钟内路径未知，极值是下界，未模拟成交与成本。"
            ),
        }
    )
