"""Causal live boundary for the frozen V5 next-hour research rule."""

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.sweep import evaluate_sweep
from btc_monitor.hourly.sweep_v5 import VERSION, confirm


def evaluate_v5(history):
    bars = history[-169:]
    result = {"version": VERSION, "candidates": [], "diagnostics": {}, "blocked": None}
    if len(bars) < 26:
        return {**result, "blocked": "need_26_closed_hours"}
    # Research confirmation helpers permit retrospective inputs. Only this live
    # boundary can call them from production, after rejecting every noncausal bar.
    if any(b.get("causal") is not True or b.get("strategy_eligible") is False for b in bars):
        return {**result, "blocked": "non_causal_inputs"}
    validation = evaluate_sweep(bars)
    if validation["blocked"]:
        return {**result, "blocked": validation["blocked"]}
    trigger = evaluate_sweep(bars[:-1])
    if trigger["blocked"]:
        return {**result, "blocked": trigger["blocked"]}
    result["diagnostics"] = {"trigger_analysis": trigger, "confirmations": []}
    for original in trigger["candidates"]:
        checked = confirm(bars, original, bars[-2]["start"])
        result["diagnostics"]["confirmations"].append(checked)
        if checked["state"] == "confirmed":
            result["candidates"].append(checked["candidate"])
    return serializable(result)
