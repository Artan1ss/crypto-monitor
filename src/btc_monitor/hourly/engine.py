"""Causal closed-hour rules. No future pivots, model fitting, orders, or V1 changes."""

from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.sweep import PARAMETERS as SWEEP_PARAMETERS
from btc_monitor.hourly.sweep import evaluate_sweep

D = Decimal
VERSION = "h1-pressure-sweep-v1.2"


def parameters(settings):
    return {
        "version": SWEEP_PARAMETERS["version"] if settings.hourly_strategy == "sweep" else VERSION,
        "hourly_strategy": settings.hourly_strategy,
        "active_signal_kinds": ["sweep_split_cvd"]
        if settings.hourly_strategy == "sweep"
        else ["pressure"]
        if settings.hourly_strategy == "pressure"
        else ["pressure", "sweep_split_cvd"],
        "flow_hours": settings.hourly_flow_hours,
        "pressure_threshold": settings.hourly_pressure_threshold,
        "price_move_atr": settings.hourly_price_move_atr,
        "level_tolerance_atr": settings.hourly_level_tolerance_atr,
        "cooldown_hours": settings.hourly_cooldown_hours,
        "pivot_left": 2,
        "pivot_right": 1,
        "atr_hours": 14,
        "decision_delay_seconds": settings.hourly_decision_delay_seconds,
        "volume_baseline": "median of prior 20 closed hours",
        "sweep_split_cvd": SWEEP_PARAMETERS,
    }


def evaluate(history, settings):
    result = _pressure_evaluate(history, settings)
    if settings.hourly_strategy == "pressure":
        return result
    sweep = evaluate_sweep(history)
    result["diagnostics"]["sweep_split_cvd"] = sweep
    if settings.hourly_strategy == "sweep":
        result.update(
            version=sweep["version"], candidates=sweep["candidates"], blocked=sweep["blocked"]
        )
        return result
    # A sweep confirmation supersedes an older pressure candidate in the same
    # direction; one hour must not create two notifications for the same setup.
    for candidate in sweep["candidates"]:
        result["candidates"] = [
            c for c in result["candidates"] if c["direction"] != candidate["direction"]
        ]
        result["candidates"].append(candidate)
    if sweep["candidates"]:
        result["blocked"] = None
    return result


def _pressure_evaluate(history, settings):
    """history includes the current bar, ordered, immutable availability-time features."""
    result = {"version": VERSION, "candidates": [], "diagnostics": {}, "blocked": None}
    if len(history) < 25:
        result["blocked"] = "need_25_closed_hours"
        return result
    bars = history[-25:]
    if (
        any(b["price"] is None for b in bars)
        or any(
            datetime.fromisoformat(b["start"]) + timedelta(hours=1)
            != datetime.fromisoformat(c["start"])
            for b, c in zip(bars, bars[1:], strict=False)
        )
        or len({b["source"] for b in bars}) != 1
        or len({tuple(b["expected_feeds"]) for b in bars}) != 1
    ):
        result["blocked"] = "incomplete_or_changed_cohort_context"
        return result
    price = [b["price"] for b in bars]
    ranges = [
        max(
            D(p["high"]) - D(p["low"]),
            abs(D(p["high"]) - D(q["close"])),
            abs(D(p["low"]) - D(q["close"])),
        )
        for q, p in zip(price, price[1:], strict=False)
    ]
    atr = sum(ranges[-14:], D(0)) / 14
    if atr <= 0:
        result["blocked"] = "zero_atr"
        return result
    current = bars[-1]
    close, high, low, open_ = (D(price[-1][k]) for k in ("close", "high", "low", "open"))
    window = bars[-settings.hourly_flow_hours :]
    if any(not b["complete"] for b in window):
        result["blocked"] = "incomplete_current_cvd_window"
        return result
    deltas, volumes, pressures = {}, {}, {}
    for market in ("spot", "linear"):
        deltas[market] = sum((D(b["aggregated"][market]["delta_btc"]) for b in window), D(0))
        volumes[market] = sum((D(b["aggregated"][market]["volume_btc"]) for b in window), D(0))
        if volumes[market] <= 0:
            result["blocked"] = "zero_volume_normalizer"
            return result
        pressures[market] = deltas[market] / volumes[market]
        if abs(pressures[market]) > 1:
            result["blocked"] = "invalid_delta_volume_ratio"
            return result
    price_change = close - D(bars[-settings.hourly_flow_hours - 1]["price"]["close"])
    prior_volume = [b["aggregated"]["linear"]["volume_btc"] for b in bars[-21:-1]]
    baseline = (
        median(D(v) for v in prior_volume) if all(v is not None for v in prior_volume) else None
    )
    volume_ratio = D(current["aggregated"]["linear"]["volume_btc"]) / baseline if baseline else None
    votes = {
        k: sum((D(b["flows"][k]["delta_btc"]) for b in window), D(0))
        for k in current["flows"]
        if ":spot:" in k
    }
    diag = {
        "atr": atr,
        "price_change": price_change,
        "price_change_atr": price_change / atr,
        "cvd_window_start": window[0]["start"],
        "cvd_window_end": current["end"],
        "spot_delta_btc": deltas["spot"],
        "futures_delta_btc": deltas["linear"],
        "spot_normalizer_btc": volumes["spot"],
        "futures_normalizer_btc": volumes["linear"],
        "spot_pressure": pressures["spot"],
        "futures_pressure": pressures["linear"],
        "futures_spot_pressure_ratio": abs(pressures["linear"] / pressures["spot"])
        if pressures["spot"]
        else None,
        "spot_venue_deltas": votes,
        "volume_ratio": volume_ratio,
        "volume_baseline_btc": baseline,
        "oi_change_pct": current["oi_change_pct"],
        "funding_fraction_mean": current["funding_fraction_mean"],
        "funding_basis": current["funding_basis"],
        "references": {},
        "cumulative_reset_used": False,
        "cvd_sampling": "sum of same-cohort closed bars, pivot CVD at bar close (not high tick)",
    }
    for direction, sign, side in (("SHORT", -1, "high"), ("LONG", 1, "low")):
        # Pivot must already have its right-hand confirmation candle before current bar.
        pivots = [
            i
            for i in range(2, len(bars) - 2)
            if (
                all(D(price[i][side]) > D(price[j][side]) for j in (i - 2, i - 1, i + 1))
                if side == "high"
                else all(D(price[i][side]) < D(price[j][side]) for j in (i - 2, i - 1, i + 1))
            )
        ]
        pivot = pivots[-1] if pivots else None
        reference = D(price[pivot][side]) if pivot is not None else None
        tolerance = atr * D(str(settings.hourly_level_tolerance_atr))
        consensus = sum(v * sign > 0 for v in votes.values()) >= 2
        pressure = pressures["spot"] * sign >= D(str(settings.hourly_pressure_threshold))
        rolling = pressure and price_change * sign <= -atr * D(str(settings.hourly_price_move_atr))
        divergence = rejected = failed = False
        pivot_delta = pivot_pressure = None
        if pivot is not None:
            between = bars[pivot + 1 :]
            if all(
                b["aggregated"]["spot"]["delta_btc"] is not None
                and b["aggregated"]["spot"]["volume_btc"] is not None
                for b in between
            ):
                pivot_delta = sum((D(b["aggregated"]["spot"]["delta_btc"]) for b in between), D(0))
                norm = sum((D(b["aggregated"]["spot"]["volume_btc"]) for b in between), D(0))
                pivot_pressure = pivot_delta / norm if norm else None
            near = high >= reference - tolerance if sign < 0 else low <= reference + tolerance
            divergence = (
                near
                and pivot_pressure is not None
                and pivot_pressure * sign >= D(str(settings.hourly_pressure_threshold))
            )
            rejected = (
                high > reference and close < reference - atr * D("0.05")
                if sign < 0
                else low < reference and close > reference + atr * D("0.05")
            )
            failed = (
                near
                and high <= reference
                and close < open_
                and high - max(open_, close) >= atr * D("0.2")
                if sign < 0
                else near
                and low >= reference
                and close > open_
                and min(open_, close) - low >= atr * D("0.2")
            )
        spot_volume = D(current["aggregated"]["spot"]["volume_btc"])
        current_pressure = (
            D(current["aggregated"]["spot"]["delta_btc"]) / spot_volume if spot_volume else D(0)
        )
        confirmed = (rejected or failed) and (
            divergence
            or rolling
            or current_pressure * sign >= D(str(settings.hourly_pressure_threshold))
        )
        watch = (rolling or divergence) and consensus
        diag["references"][direction] = {
            "pivot_index": pivot,
            "pivot_start": bars[pivot]["start"] if pivot is not None else None,
            "pivot_confirmed_at": bars[pivot + 1]["end"] if pivot is not None else None,
            "level": reference,
            "tolerance": tolerance,
            "pivot_to_now_delta_btc": pivot_delta,
            "pivot_to_now_pressure": pivot_pressure,
            "rolling_pressure_divergence": rolling,
            "pivot_divergence": divergence,
            "breakout_rejected": rejected,
            "failed_retest": failed,
            "spot_consensus": consensus,
        }
        if not (confirmed or watch):
            continue
        reasons = []
        if rolling:
            reasons.append("价格与现货主动成交压力背离")
        if divergence:
            reasons.append("已确认摆动点与现货 CVD 背离")
        if rejected:
            reasons.append("突破关键位后收盘回到区间内")
        if failed:
            reasons.append("关键位附近反弹/回踩失败")
        if volume_ratio and volume_ratio >= D("1.3"):
            reasons.append("合约成交量放大")
        oi = D(current["oi_change_pct"]) if current["oi_change_pct"] is not None else None
        funding = (
            D(current["funding_fraction_mean"])
            if current["funding_fraction_mean"] is not None
            else None
        )
        context = "OI/费率数据不足"
        if oi is not None and funding is not None:
            context = (
                "OI增加且持仓成本偏向反方向"
                if oi > 0 and funding * sign < 0
                else "杠杆背景中性/混合"
            )
        extreme = (
            max(high, reference)
            if reference is not None and sign < 0
            else min(low, reference)
            if reference is not None
            else high
            if sign < 0
            else low
        )
        stop = extreme - sign * atr * D("0.2")
        result["candidates"].append(
            serializable(
                {
                    "direction": direction,
                    "tier": "CONFIRMED" if confirmed else "WATCH",
                    "kind": "breakout_rejection"
                    if rejected
                    else "failed_retest"
                    if failed
                    else "cvd_divergence",
                    "bar_close": close,
                    "reference_level": reference,
                    "invalidation": stop,
                    "reasons": reasons,
                    "context": context,
                    "warnings": [
                        "观察提醒不是交易指令",
                        "固定交易所集合，不代表全市场",
                        "资金费率为未统一结算周期的报告值；OI不代表多空方向",
                    ]
                    + (["现货方向一致性不足"] if not consensus else []),
                }
            )
        )
    result["diagnostics"] = serializable(diag)
    return result
