"""V1 candidate morphology filter. Example-guided research, never production."""

import copy
from datetime import datetime

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.sweep import D, evaluate_sweep
from btc_monitor.hourly.sweep_v2 import flow

VERSION = "h1-v1-morphology-sweep-v3"
PARAMETERS = {
    "version": VERSION,
    "base": "unchanged V1 anchor and CVD split, no older anchor search",
    "minimum_anchor_age_hours": 4,
    "minimum_departure_atr": "1",
    "minimum_trigger_range_atr": "1",
    "minimum_trigger_range_price_fraction": "0.0035",
    "minimum_sweep_depth_atr": "0.05",
    "minimum_reclaim_atr": "0.1",
    "immediate_close_location": "0.65",
    "immediate_rejection_wick_fraction": "0.35",
    "immediate_directional_body_fraction": "0.25",
    "confirmation_wait_hours": 2,
    "delayed_close_location": "0.65",
    "delayed_body_atr": "0.5",
    "maximum_further_sweep_atr": "0.5",
    "delayed_confirmation": "directional displacement close beyond original sweep open and midpoint",
    "pending_cancel": "close loses original anchor, excessive new extreme, gaps, or CVD split lost",
    "stop_buffer_atr": "0.2",
    "scope": "example_guided_local_research_no_telegram",
}


def candle_metrics(price, sign):
    o, h, l, c = (D(price[k]) for k in ("open", "high", "low", "close"))
    width = h - l
    if width <= 0:
        return {"range": D(0), "location": D(0), "wick": D(0), "body": D(0)}
    return {
        "range": width,
        "location": (c - l if sign > 0 else h - c) / width,
        "wick": (min(o, c) - l if sign > 0 else h - max(o, c)) / width,
        "body": sign * (c - o) / width,
    }


def assess(history, candidate, atr):
    """Inspect only the original V1 sweep prefix; do not inspect later outcomes."""
    sign = 1 if candidate["direction"] == "LONG" else -1
    side = "low" if sign > 0 else "high"
    ref = candidate["sweep_diagnostics"]
    pivot = next(i for i, b in enumerate(history) if b["start"] == ref["pivot_start"])
    p = history[-1]["price"]
    level = D(ref["level"])
    m = candle_metrics(p, sign)
    departure = max(
        (sign * (D(b["price"]["high" if sign > 0 else "low"]) - level)
         for b in history[pivot + 1:-1]), default=D(0)
    ) / atr
    age = (datetime.fromisoformat(history[-1]["start"]) - datetime.fromisoformat(ref["pivot_start"])).total_seconds() / 3600
    metrics = {
        "anchor_age_hours": age, "departure_atr": departure,
        "range_atr": m["range"] / atr,
        "range_price_fraction": m["range"] / D(p["close"]),
        "sweep_depth_atr": sign * (level - D(p[side])) / atr,
        "reclaim_atr": sign * (D(p["close"]) - level) / atr,
        "candle": m,
    }
    checks = {
        "anchor_too_recent": age >= PARAMETERS["minimum_anchor_age_hours"],
        "insufficient_swing_departure": departure >= D(PARAMETERS["minimum_departure_atr"]),
        "trigger_range_below_atr": metrics["range_atr"] >= D(PARAMETERS["minimum_trigger_range_atr"]),
        "trigger_absolute_range_too_small": metrics["range_price_fraction"] >= D(PARAMETERS["minimum_trigger_range_price_fraction"]),
        "sweep_too_small": metrics["sweep_depth_atr"] >= D(PARAMETERS["minimum_sweep_depth_atr"]),
        "reclaim_too_small": metrics["reclaim_atr"] >= D(PARAMETERS["minimum_reclaim_atr"]),
    }
    rejected = [key for key, passed in checks.items() if not passed]
    immediate = m["location"] >= D(PARAMETERS["immediate_close_location"]) and (
        m["wick"] >= D(PARAMETERS["immediate_rejection_wick_fraction"])
        or m["body"] >= D(PARAMETERS["immediate_directional_body_fraction"])
    )
    return serializable({"metrics": metrics, "failed_checks": rejected, "immediate": immediate})


def confirm(history, original, sweep_start):
    """Called on a closed prefix. Return pending/cancelled/confirmed, never future bars."""
    sweep_index = next(i for i, b in enumerate(history) if b["start"] == sweep_start)
    base = history[:sweep_index + 1]
    preflight = evaluate_sweep(base, research=True)
    # Require this exact frozen V1 anchor, rather than trusting arbitrary candidates.
    match = next((c for c in preflight["candidates"] if c["event_key"] == original["event_key"]), None)
    if match is None:
        return {"state": "cancelled", "reason": "not_original_v1_candidate"}
    validation = evaluate_sweep(history, research=True)
    if validation["blocked"]:
        return {"state": "cancelled", "reason": validation["blocked"]}
    atr = D(preflight["diagnostics"]["atr"])
    gate = assess(base, match, atr)
    result = {"state": "pending", "shape": gate, "reason": None}
    if gate["failed_checks"]:
        return {**result, "state": "cancelled", "reason": gate["failed_checks"][0]}
    sign = 1 if match["direction"] == "LONG" else -1
    side = "low" if sign > 0 else "high"
    trigger, current = base[-1]["price"], history[-1]["price"]
    wait = len(history) - len(base)
    if wait > PARAMETERS["confirmation_wait_hours"]:
        return {**result, "state": "cancelled", "reason": "confirmation_expired"}
    pivot_start = match["sweep_diagnostics"]["pivot_start"]
    segment = [b for b in history if b["start"] > pivot_start]
    # Same fixed cohort, complete flow interval and contiguous closed hours.
    if any(not b.get("complete") or b.get("closed") is False for b in segment) or any(
        datetime.fromisoformat(a["end"]) != datetime.fromisoformat(b["start"])
        or a["source"] != b["source"] or a["expected_feeds"] != b["expected_feeds"]
        for a, b in zip(segment, segment[1:])
    ):
        return {**result, "state": "cancelled", "reason": "confirmation_data_gap_or_cohort_change"}
    level = D(match["reference_level"])
    for b in history[sweep_index:]:
        if sign * (D(b["price"]["close"]) - level) <= 0:
            return {**result, "state": "cancelled", "reason": "anchor_lost_on_close"}
    extreme = (min if sign > 0 else max)(D(b["price"][side]) for b in history[sweep_index:])
    if sign * (D(trigger[side]) - extreme) / atr > D(PARAMETERS["maximum_further_sweep_atr"]):
        return {**result, "state": "cancelled", "reason": "further_sweep_too_deep"}
    flows = flow(segment)
    if flows is None:
        return {**result, "state": "cancelled", "reason": "invalid_confirmation_flow"}
    deltas = {market: flows[market]["pivot_to_now_delta_btc"] for market in ("spot", "linear")}
    if deltas["spot"] * sign < 0 or deltas["linear"] * sign >= 0:
        return {**result, "state": "cancelled", "reason": "cvd_split_lost_at_confirmation"}
    m = candle_metrics(current, sign)
    delayed = wait > 0 and (
        sign * (D(current["close"]) - D(trigger["open"])) > 0
        and sign * (D(current["close"]) - (D(trigger["high"]) + D(trigger["low"])) / 2) > 0
        and sign * (D(current["close"]) - D(current["open"])) / atr >= D(PARAMETERS["delayed_body_atr"])
        and m["location"] >= D(PARAMETERS["delayed_close_location"])
    )
    if not ((wait == 0 and gate["immediate"]) or delayed):
        return {**result, "state": "cancelled" if wait == PARAMETERS["confirmation_wait_hours"] else "pending", "reason": "confirmation_expired" if wait == PARAMETERS["confirmation_wait_hours"] else "waiting_for_displacement"}
    candidate = copy.deepcopy(match)
    candidate.update(
        kind="v1_morphology_sweep_v3", event_key=f"{VERSION}:{match['event_key']}",
        bar_close=str(current["close"]), invalidation=str(extreme - sign * atr * D(".2")),
        morphology=serializable({**gate, "sweep_start": sweep_start, "confirmation_start": history[-1]["start"], "confirmation_wait_hours": wait,
            "confirmation_kind": "same_bar_rejection" if wait == 0 else "delayed_displacement",
            "confirmation_candle": m, "event_extreme": extreme, "confirmation_flow_delta_btc": deltas,
            "original_v1_event_key": match["event_key"], "atr_at_sweep": atr}),
    )
    candidate["reasons"] += ["明显摆动位首次扫回，波动幅度达标", "当根拒绝形态确认" if wait == 0 else "后续位移K线确认（不回填到扫点时间）"]
    return serializable({**result, "state": "confirmed", "candidate": candidate})
