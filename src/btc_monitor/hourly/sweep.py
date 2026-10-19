"""Confirmed swing sweep + opposing spot/futures CVD, without CVD resets.

The price pivot is frozen before the sweep. CVD is sampled at the SAME pivot
bar close in both markets; hourly data cannot establish CVD at the wick tick.
Research mode accepts retrospective inputs but never makes them causal.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from btc_monitor.archive.types import serializable


def D(value):
    return Decimal(str(value))


VERSION = "h1-sweep-split-cvd-v1"
PARAMETERS = {
    "version": VERSION,
    "context_hours": 168,
    "pivot_left": 2,
    "pivot_right": 1,
    "anchor_selection": "latest reclaimed confirmed pivot, not previously pierced",
    "cvd_sampling": "same price-pivot close to current close, no reset",
    "stop_buffer_atr": "0.2",
    "score_version": "h1-sweep-evidence-v1",
}


def evaluate_sweep(history, *, research=False):
    result = {"version": VERSION, "candidates": [], "diagnostics": {}, "blocked": None}
    bars = history[-168:]
    if len(bars) < 25:
        result["blocked"] = "need_25_closed_hours"
        return result
    if not research and any(
        b.get("causal") is not True or b.get("strategy_eligible") is False for b in bars
    ):
        result["blocked"] = "non_causal_inputs"
        return result
    # Price continuity is required for pivot selection/ATR. Flow completeness is
    # checked separately over the full selected anchor-to-current interval.
    if (
        any(not b.get("price") or b.get("closed") is False for b in bars)
        or any(
            datetime.fromisoformat(a["end"]) != datetime.fromisoformat(b["start"])
            or datetime.fromisoformat(a["start"]) + timedelta(hours=1)
            != datetime.fromisoformat(a["end"])
            for a, b in zip(bars, bars[1:], strict=False)
        )
        or datetime.fromisoformat(bars[-1]["start"]) + timedelta(hours=1)
        != datetime.fromisoformat(bars[-1]["end"])
    ):
        result["blocked"] = "price_gap_or_unclosed_bar"
        return result
    if (
        any(not b.get("expected_feeds") for b in bars)
        or len({(b["source"], tuple(b["expected_feeds"])) for b in bars}) != 1
    ):
        result["blocked"] = "changed_cohort"
        return result
    price = [b["price"] for b in bars]
    if any(
        not all(D(p[k]).is_finite() for k in ("open", "high", "low", "close"))
        or not D(0)
        < D(p["low"])
        <= min(D(p["open"]), D(p["close"]))
        <= max(D(p["open"]), D(p["close"]))
        <= D(p["high"])
        for p in price
    ):
        result["blocked"] = "invalid_ohlc"
        return result
    atr = (
        sum(
            (
                max(
                    D(p["high"]) - D(p["low"]),
                    abs(D(p["high"]) - D(q["close"])),
                    abs(D(p["low"]) - D(q["close"])),
                )
                for q, p in zip(price[-15:-1], price[-14:], strict=True)
            ),
            D(0),
        )
        / 14
    )
    if atr <= 0:
        result["blocked"] = "zero_atr"
        return result
    current = bars[-1]
    diagnostics = {"atr": atr, "research_only": research, "references": {}}
    for direction, sign, side in (("LONG", 1, "low"), ("SHORT", -1, "high")):
        pivots = [
            i
            for i in range(2, len(bars) - 2)
            if all(
                (D(price[j][side]) - D(price[i][side])) * sign > 0 for j in (i - 2, i - 1, i + 1)
            )
        ]
        # Freeze the latest reclaimed *still-intact* structure before checking CVD.
        # Never select a more profitable/divergent older pivot after seeing flow.
        intact = [
            i
            for i in pivots
            if all((D(p[side]) - D(price[i][side])) * sign >= 0 for p in price[i + 1 : -1])
        ]
        swept = [
            i
            for i in intact
            if (D(price[-1][side]) - D(price[i][side])) * sign < 0
            and (D(price[-1]["close"]) - D(price[i][side])) * sign > 0
        ]
        ref = {
            "eligible_pivots": [bars[i]["start"] for i in intact],
            "reclaimed_pivots": [bars[i]["start"] for i in swept],
            "matched": False,
        }
        diagnostics["references"][direction] = ref
        if not swept:
            ref["blocked"] = "no_first_sweep_and_close_reclaim"
            continue
        pivot = swept[-1]
        level = D(price[pivot][side])
        segment = bars[pivot + 1 :]
        ref.update(
            pivot_start=bars[pivot]["start"],
            pivot_confirmed_at=bars[pivot + 1]["end"],
            level=level,
            comparison_start=bars[pivot]["end"],
            comparison_end=current["end"],
            compared_hours=len(segment),
            cvd_reset=False,
            cvd_sampling=PARAMETERS["cvd_sampling"],
        )
        if any(not b.get("complete") for b in bars[pivot:]):
            ref["blocked"] = "incomplete_anchor_cvd_interval"
            continue
        flows = {}
        valid = True
        for market in ("spot", "linear"):
            values = [b["aggregated"][market] for b in segment]
            if any(v.get("delta_btc") is None or v.get("volume_btc") is None for v in values):
                valid = False
                break
            if any(
                not D(v["delta_btc"]).is_finite()
                or not D(v["volume_btc"]).is_finite()
                or D(v["volume_btc"]) <= 0
                or abs(D(v["delta_btc"])) > D(v["volume_btc"])
                for v in values
            ):
                valid = False
                break
            delta = sum((D(v["delta_btc"]) for v in values), D(0))
            volume = sum((D(v["volume_btc"]) for v in values), D(0))
            flows[market] = {
                "pivot_to_now_delta_btc": delta,
                "normalizer_btc": volume,
                "pressure": delta / volume,
            }
        ref["flows"] = flows
        if not valid:
            ref["blocked"] = "invalid_delta_or_volume"
            continue
        # No pressure threshold: this tests *relative* CVD extrema, not buying
        # pressure. Flat spot is allowed, flat futures is NOT a lower/higher low.
        split = (
            flows["spot"]["pivot_to_now_delta_btc"] * sign >= 0
            and flows["linear"]["pivot_to_now_delta_btc"] * sign < 0
        )
        ref.update(
            matched=split,
            blocked=None if split else "spot_futures_extrema_not_split",
            sweep_depth_atr=abs(D(price[-1][side]) - level) / atr,
            reclaim_atr=(D(price[-1]["close"]) - level) * sign / atr,
        )
        if not split:
            continue
        venue_deltas = {
            k: sum((D(b["flows"][k]["delta_btc"]) for b in segment), D(0))
            for k in current["flows"]
            if ":spot:" in k
        }
        ref["spot_venue_deltas"] = venue_deltas
        baseline_values = [b["aggregated"]["linear"].get("volume_btc") for b in bars[-21:-1]]
        baseline = (
            median(D(v) for v in baseline_values)
            if all(v is not None and D(v) > 0 for v in baseline_values)
            else None
        )
        ratio = D(current["aggregated"]["linear"]["volume_btc"]) / baseline if baseline else None
        ref["volume_ratio"] = ratio
        components = [
            {"key": "sweep_reclaim", "points": 30, "max": 30},
            {"key": "split_cvd", "points": 30, "max": 30},
            {
                "key": "reclaim_strength",
                "points": round(20 * min(D(1), ref["reclaim_atr"])),
                "max": 20,
            },
            {
                "key": "spot_venues",
                "points": round(
                    10 * sum(v * sign >= 0 for v in venue_deltas.values()) / len(venue_deltas)
                )
                if venue_deltas
                else 0,
                "max": 10,
            },
            {
                "key": "volume",
                "points": round(10 * min(D(1), max(D(0), (ratio - 1) / D("0.5")))) if ratio else 0,
                "max": 10,
            },
        ]
        result["candidates"].append(
            {
                "direction": direction,
                "tier": "CONFIRMED",
                "kind": "sweep_split_cvd",
                "bar_close": D(price[-1]["close"]),
                "reference_level": level,
                "invalidation": D(price[-1][side]) - sign * atr * D("0.2"),
                "event_key": f"{VERSION}:{direction}:{bars[pivot]['start']}",
                "reasons": [
                    "首次扫过已确认摆动点后收盘收回",
                    "价格与合约CVD突破前极值，现货CVD未突破同一锚点极值",
                ],
                "context": "OI和费率仅作背景，不作为方向确认",
                "warnings": [
                    "证据分不是胜率",
                    "固定交易所集合，不代表全市场",
                    "CVD按价格摆动小时收盘取样，非影线触及瞬间",
                ],
                "sweep_diagnostics": ref,
                "evidence_score": {
                    "version": PARAMETERS["score_version"],
                    "score": sum(c["points"] for c in components),
                    "max_score": 100,
                    "components": components,
                    "basis": "decision_inputs_only_not_probability",
                },
            }
        )
    result["diagnostics"] = diagnostics
    return serializable(result)
