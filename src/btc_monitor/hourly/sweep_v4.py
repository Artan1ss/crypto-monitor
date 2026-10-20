"""V1 shape subset with mandatory immediately-next closed candle confirmation."""

import copy

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.sweep import D, evaluate_sweep
from btc_monitor.hourly.sweep_v2 import flow
from btc_monitor.hourly.sweep_v3 import PARAMETERS as V3_PARAMETERS, assess, candle_metrics

VERSION = "h1-v1-next-candle-sweep-v4"
PARAMETERS = {
    **{k: v for k, v in V3_PARAMETERS.items() if not k.startswith("immediate_") and not k.startswith("delayed_")},
    "version": VERSION,
    "confirmation_wait_hours": 1,
    "same_bar_confirmation": False,
    "next_candle_body_atr": "0.25",
    "next_candle_progress_atr": "0.25",
    "next_candle_close_location": "0.65",
    "next_candle_reclaim_atr": "0.1",
    "confirmation": "exactly next closed hour: directional body, progress vs sweep close, reclaim and CVD retained",
    "pending_cancel": "next candle fails any check: cancel, no second chance",
}


def confirm(history, original, sweep_start):
    index = next(i for i, b in enumerate(history) if b["start"] == sweep_start)
    base = history[:index + 1]
    preflight = evaluate_sweep(base, research=True)
    candidate = next((c for c in preflight["candidates"] if c["event_key"] == original["event_key"]), None)
    if candidate is None:
        return {"state": "cancelled", "reason": "not_original_v1_candidate"}
    atr = D(preflight["diagnostics"]["atr"])
    shape = assess(base, candidate, atr)
    result = {"state": "pending", "shape": shape, "reason": "must_wait_for_next_closed_candle"}
    if shape["failed_checks"]:
        return {**result, "state": "cancelled", "reason": shape["failed_checks"][0]}
    wait = len(history) - len(base)
    if wait == 0:
        return result
    if wait != 1:
        return {**result, "state": "cancelled", "reason": "next_candle_window_expired"}
    validated = evaluate_sweep(history, research=True)
    if validated["blocked"]:
        return {**result, "state": "cancelled", "reason": validated["blocked"]}
    sign = 1 if candidate["direction"] == "LONG" else -1
    side = "low" if sign > 0 else "high"
    trigger, current = base[-1]["price"], history[-1]["price"]
    level = D(candidate["reference_level"])
    m = candle_metrics(current, sign)
    extreme = (min if sign > 0 else max)(D(p[side]) for p in (trigger, current))
    metrics = {
        "body_atr": sign * (D(current["close"]) - D(current["open"])) / atr,
        "progress_atr": sign * (D(current["close"]) - D(trigger["close"])) / atr,
        "reclaim_atr": sign * (D(current["close"]) - level) / atr,
        "close_location": m["location"],
        "further_sweep_atr": sign * (D(trigger[side]) - extreme) / atr,
    }
    segment = [b for b in history if b["start"] > candidate["sweep_diagnostics"]["pivot_start"]]
    flows = flow(segment)
    deltas = {market: flows[market]["pivot_to_now_delta_btc"] for market in ("spot", "linear")} if flows else None
    checks = {
        "next_candle_not_directional_reversal": metrics["body_atr"] >= D(PARAMETERS["next_candle_body_atr"]),
        "next_candle_no_close_progress": metrics["progress_atr"] >= D(PARAMETERS["next_candle_progress_atr"]),
        "next_candle_reclaim_failed": metrics["reclaim_atr"] >= D(PARAMETERS["next_candle_reclaim_atr"]),
        "next_candle_close_too_weak": metrics["close_location"] >= D(PARAMETERS["next_candle_close_location"]),
        "further_sweep_too_deep": metrics["further_sweep_atr"] <= D(PARAMETERS["maximum_further_sweep_atr"]),
        "invalid_confirmation_flow": flows is not None,
        "cvd_split_lost_at_confirmation": deltas is not None and deltas["spot"] * sign >= 0 and deltas["linear"] * sign < 0,
    }
    failures = [key for key, passed in checks.items() if not passed]
    result.update(next_candle=serializable(metrics), failed_checks=failures)
    if failures:
        return {**result, "state": "cancelled", "reason": failures[0]}
    c = copy.deepcopy(candidate)
    c.update(kind="v1_next_candle_sweep_v4", event_key=f"{VERSION}:{candidate['event_key']}",
        bar_close=str(current["close"]), invalidation=str(extreme - sign * atr * D(".2")),
        morphology=serializable({**shape, "sweep_start": sweep_start, "confirmation_start": history[-1]["start"],
            "confirmation_wait_hours": 1, "confirmation_kind": "mandatory_next_candle_reversal",
            "next_candle_metrics": metrics, "event_extreme": extreme, "confirmation_flow_delta_btc": deltas,
            "original_v1_event_key": candidate["event_key"], "atr_at_sweep": atr}))
    c["reasons"] += ["下一根已收盘K线实体反转、收回关键位且背离保留（非当根确认）"]
    return serializable({**result, "state": "confirmed", "reason": None, "candidate": c})
