"""Next-candle price confirmation with anchored CVD transition, research only."""

import copy

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.sweep import D, evaluate_sweep
from btc_monitor.hourly.sweep_v2 import flow
from btc_monitor.hourly import sweep_v4

VERSION = "h1-v1-cvd-transition-sweep-v5"
PARAMETERS = {
    **sweep_v4.PARAMETERS,
    "version": VERSION,
    "confirmation": "V4 next-candle price gates unchanged; CVD split at sweep, spot anchor retained, futures may rotate",
    "cvd_confirmation": "spot cumulative delta retains signal-side sign at same anchor; futures either still split or rotate toward signal",
    "spot_guard": "anchor-to-confirmation delta cannot cross against direction; no arbitrary new pressure threshold",
    "scope": "example_guided_local_research_no_telegram",
}


def confirm(history, original, sweep_start):
    # V4 checks the frozen V1 candidate, exact next hour, closed/contiguous prices,
    # fixed cohort, and validated full CVD interval. Do not relax any price gate.
    result = copy.deepcopy(sweep_v4.confirm(history, original, sweep_start))
    if "next_candle" not in result:
        return result
    segment = [b for b in history if b["start"] > original["sweep_diagnostics"]["pivot_start"]]
    full, trigger, next_hour = flow(segment), flow(segment[:-1]), flow(segment[-1:])
    if full is None or trigger is None or next_hour is None:
        return {**result, "state": "cancelled", "reason": "invalid_confirmation_flow"}
    sign = 1 if original["direction"] == "LONG" else -1
    delta = lambda data, market: data[market]["pivot_to_now_delta_btc"]
    spot_held = delta(full, "spot") * sign >= 0
    split_held = delta(full, "linear") * sign < 0
    rotated = delta(full, "linear") * sign >= 0 and delta(next_hour, "linear") * sign > 0
    audit = serializable({
        "trigger_flows": trigger, "confirmation_hour_flows": next_hour, "anchor_to_confirmation_flows": full,
        "spot_anchor_direction_retained": spot_held,
        "futures_state": "split_persists" if split_held else "rotated_toward_signal" if rotated else "unconfirmed",
        "spot_hour_opposes_signal": delta(next_hour, "spot") * sign < 0,
        "sampling": "same frozen price-pivot close, no CVD reset; hourly close samples only",
    })
    failures = [f for f in result.get("failed_checks", []) if f != "cvd_split_lost_at_confirmation"]
    if not spot_held:
        failures.append("spot_anchor_divergence_invalidated")
    if not (split_held or rotated):
        failures.append("futures_neither_split_nor_confirmed_rotation")
    result.update(cvd_transition=audit, failed_checks=failures)
    if failures:
        return {**result, "state": "cancelled", "reason": failures[0]}
    index = next(i for i, b in enumerate(history) if b["start"] == sweep_start)
    atr = D(evaluate_sweep(history[:index + 1], research=True)["diagnostics"]["atr"])
    side = "low" if sign > 0 else "high"
    extreme = (min if sign > 0 else max)(D(b["price"][side]) for b in history[index:])
    candidate = copy.deepcopy(original)
    candidate.update(
        kind="v1_cvd_transition_sweep_v5", event_key=f"{VERSION}:{original['event_key']}",
        bar_close=str(history[-1]["price"]["close"]), invalidation=str(extreme - sign * atr * D(".2")),
        morphology=serializable({**result["shape"], "sweep_start": sweep_start,
            "confirmation_start": history[-1]["start"], "confirmation_wait_hours": 1,
            "confirmation_kind": "mandatory_next_candle_reversal", "next_candle_metrics": result["next_candle"],
            "event_extreme": extreme, "atr_at_sweep": atr, "original_v1_event_key": original["event_key"],
            "confirmation_flow_delta_btc": {m: delta(full, m) for m in ("spot", "linear")},
            "cvd_transition": audit}),
    )
    candidate["reasons"] += ["下一根价格反向确认，现货同锚点背离保留",
        "合约主动成交已转向信号方向（背离兑现）" if rotated else "合约CVD分化仍保持"]
    return serializable({**result, "state": "confirmed", "reason": None, "candidate": candidate})
