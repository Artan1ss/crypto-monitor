"""Independent research-only major swing sweep; never replaces production V1."""

from datetime import datetime
from statistics import median

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.sweep import D, evaluate_sweep

VERSION = "h1-major-sweep-split-cvd-v2"
PARAMETERS = {
    "version": VERSION,
    "context_hours": 168,
    "pivot_left": 3,
    "pivot_right": 2,
    "minimum_pivot_age_hours": 6,
    "maximum_pivot_age_hours": 72,
    "minimum_departure_atr": "1",
    "minimum_sweep_depth_atr": "0.05",
    "minimum_reclaim_atr": "0.1",
    "minimum_close_location": "0.65",
    "prior_closed_flow_split": True,
    "strict_spot_higher_lower_extreme": True,
    "anchor_selection": "latest reclaimed intact major price pivot, before checking CVD",
    "cvd_sampling": "same price-pivot close; prior prefix and trigger prefix, no reset",
    "stop_buffer_atr": "0.2",
    "score_version": "h1-major-sweep-evidence-v2",
    "scope": "research_only_no_production_or_telegram",
}


def flow(segment):
    result = {}
    if not segment or any(not b.get("complete") for b in segment):
        return None
    for market in ("spot", "linear"):
        rows = [b["aggregated"][market] for b in segment]
        if any(v.get("delta_btc") is None or v.get("volume_btc") is None for v in rows):
            return None
        if any(
            not D(v["delta_btc"]).is_finite()
            or not D(v["volume_btc"]).is_finite()
            or D(v["volume_btc"]) <= 0
            or abs(D(v["delta_btc"])) > D(v["volume_btc"])
            for v in rows
        ):
            return None
        delta = sum((D(v["delta_btc"]) for v in rows), D(0))
        volume = sum((D(v["volume_btc"]) for v in rows), D(0))
        result[market] = {
            "pivot_to_now_delta_btc": delta,
            "normalizer_btc": volume,
            "pressure": delta / volume,
        }
    return result


def evaluate(history, *, research=False):
    # Reuse frozen V1's validation/ATR without altering its implementation.
    preflight = evaluate_sweep(history, research=research)
    result = {
        "version": VERSION,
        "candidates": [],
        "blocked": preflight["blocked"],
        "diagnostics": {"references": {}},
    }
    if result["blocked"]:
        return result
    bars = history[-168:]
    prices = [b["price"] for b in bars]
    current = prices[-1]
    atr = D(preflight["diagnostics"]["atr"])
    result["diagnostics"]["atr"] = atr
    for direction, sign, side in (("LONG", 1, "low"), ("SHORT", -1, "high")):
        ref = {"matched": False, "blocked": None}
        result["diagnostics"]["references"][direction] = ref
        major, opposite = [], []
        for i in range(3, len(bars) - 3):
            age = (
                datetime.fromisoformat(bars[-1]["start"]) - datetime.fromisoformat(bars[i]["start"])
            ).total_seconds() / 3600
            if not 6 <= age <= 72:
                continue
            neighbors = (i - 3, i - 2, i - 1, i + 1, i + 2)
            if all((D(prices[j][side]) - D(prices[i][side])) * sign > 0 for j in neighbors):
                level = D(prices[i][side])
                intact = all((D(p[side]) - level) * sign >= 0 for p in prices[i + 1 : -1])
                departure = (
                    max(
                        (
                            (D(p["high"] if sign > 0 else p["low"]) - level) * sign
                            for p in prices[i + 1 : -1]
                        ),
                        default=D(0),
                    )
                    / atr
                )
                if intact and departure >= 1:
                    major.append((i, departure))
            other_side = "high" if sign > 0 else "low"
            if all(
                (D(prices[i][other_side]) - D(prices[j][other_side])) * sign > 0 for j in neighbors
            ):
                opposite.append(D(prices[i][other_side]))
        reclaimed = [
            (i, departure)
            for i, departure in major
            if (D(current[side]) - D(prices[i][side])) * sign < 0
            and (D(current["close"]) - D(prices[i][side])) * sign > 0
        ]
        ref["eligible_pivots"] = [bars[i]["start"] for i, _ in major]
        if not reclaimed:
            ref["blocked"] = "no_first_major_sweep_reclaim"
            continue
        pivot, departure = reclaimed[-1]
        level = D(prices[pivot][side])
        ref.update(
            pivot_start=bars[pivot]["start"],
            pivot_confirmed_at=bars[pivot + 2]["end"],
            level=level,
            compared_hours=len(bars) - pivot - 1,
            comparison_start=bars[pivot]["end"],
            comparison_end=bars[-1]["end"],
            cvd_reset=False,
            departure_atr=departure,
            sweep_depth_atr=abs(D(current[side]) - level) / atr,
            reclaim_atr=(D(current["close"]) - level) * sign / atr,
        )
        full = flow(bars[pivot + 1 :]) if bars[pivot].get("complete") else None
        prior = flow(bars[pivot + 1 : -1])
        ref.update(flows=full, prior_flows=prior)
        if full is None or prior is None:
            ref["blocked"] = "incomplete_or_invalid_anchor_cvd"
            continue
        if not (
            full["spot"]["pivot_to_now_delta_btc"] * sign > 0
            and full["linear"]["pivot_to_now_delta_btc"] * sign < 0
        ):
            ref["blocked"] = "strict_trigger_cvd_split_absent"
            continue
        if not (
            prior["spot"]["pivot_to_now_delta_btc"] * sign > 0
            and prior["linear"]["pivot_to_now_delta_btc"] * sign < 0
        ):
            ref["blocked"] = "prior_closed_flow_split_absent"
            continue
        width = D(current["high"]) - D(current["low"])
        location = (
            (
                (D(current["close"]) - D(current["low"]))
                if sign > 0
                else (D(current["high"]) - D(current["close"]))
            )
            / width
            if width
            else D(0)
        )
        ref["directional_close_location"] = location
        if ref["sweep_depth_atr"] < D("0.05"):
            ref["blocked"] = "sweep_too_small"
            continue
        if ref["reclaim_atr"] < D("0.1") or location < D("0.65"):
            ref["blocked"] = "weak_reclaim_or_close_location"
            continue
        ref["matched"] = True
        volumes = [
            D(b["aggregated"]["linear"]["volume_btc"])
            for b in bars[-21:-1]
            if b["aggregated"]["linear"].get("volume_btc")
        ]
        ratio = (
            D(bars[-1]["aggregated"]["linear"]["volume_btc"]) / median(volumes)
            if volumes and median(volumes)
            else D(0)
        )
        components = [
            {"key": "major_swing", "points": 25, "max": 25},
            {"key": "strict_cvd_split", "points": 25, "max": 25},
            {"key": "prior_closed_flow_split", "points": 15, "max": 15},
            {
                "key": "sweep_and_reclaim_quality",
                "points": round(20 * min(D(1), location)),
                "max": 20,
            },
            {
                "key": "relative_volume",
                "points": round(15 * min(D(1), max(D(0), ratio - 1))),
                "max": 15,
            },
        ]
        result["candidates"].append(
            {
                "direction": direction,
                "tier": "CONFIRMED",
                "kind": "major_sweep_split_cvd_v2",
                "bar_close": D(current["close"]),
                "reference_level": level,
                "invalidation": D(current[side]) - sign * atr * D("0.2"),
                "sweep_extreme": D(current[side]),
                "atr": atr,
                "opposing_structure_levels": opposite,
                "event_key": f"{VERSION}:{direction}:{bars[pivot]['start']}",
                "sweep_diagnostics": ref,
                "evidence_score": {
                    "version": PARAMETERS["score_version"],
                    "score": sum(c["points"] for c in components),
                    "max_score": 100,
                    "components": components,
                    "basis": "decision_inputs_not_probability",
                },
                "reasons": [
                    "首次扫过已确认主要摆动（3左2右、6–72H、曾离开至少1ATR）并收回",
                    "扫点前现货/合约CVD已分化，触发收盘后分化仍保持",
                    "扫点深度≥0.05ATR、收回≥0.1ATR，收盘位于方向侧35%区间",
                ],
                "warnings": [
                    "V2独立研究，未替换线上V1",
                    "证据分不是胜率",
                    "历史数据事后获取，非当时可用证明",
                ],
            }
        )
    return serializable(result)
