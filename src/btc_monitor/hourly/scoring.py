"""Versioned research scores, NOT calibrated probabilities or order sizing."""

from decimal import Decimal

VERSION = "h1-evidence-score-v1"
D = Decimal


def confidence(snapshot):
    d, c = snapshot.get("diagnostics", {}), snapshot.get("candidate", {})
    direction = c.get("direction")
    sign = 1 if direction == "LONG" else -1
    ref = d.get("references", {}).get(direction, {})
    threshold = D(str(snapshot.get("parameters", {}).get("pressure_threshold", "0.03")))
    votes = d.get("spot_venue_deltas", {})
    pressure = max(D(0), D(str(d.get("spot_pressure") or 0)) * sign)
    volume = D(str(d.get("volume_ratio") or 0))
    oi, funding = d.get("oi_change_pct"), d.get("funding_fraction_mean")
    parts = [
        (
            "price_confirmation",
            "价格拒绝/回踩确认",
            25,
            25 if ref.get("breakout_rejected") or ref.get("failed_retest") else 0,
        ),
        (
            "divergence",
            "摆动点/滚动背离",
            20,
            20
            if ref.get("pivot_divergence")
            else 12
            if ref.get("rolling_pressure_divergence")
            else 0,
        ),
        (
            "spot_pressure",
            "现货主动压力强度",
            15,
            round(15 * min(D(1), pressure / (2 * threshold))) if threshold > 0 else 0,
        ),
        (
            "spot_consensus",
            "现货交易所方向一致性",
            15,
            round(15 * sum(D(str(v)) * sign > 0 for v in votes.values()) / len(votes))
            if votes
            else 0,
        ),
        ("volume", "相对成交量", 10, round(10 * min(D(1), max(D(0), (volume - 1) / D("0.5"))))),
        (
            "futures_context",
            "合约同向压力/现货主导背离",
            10,
            10
            if d.get("futures_pressure") is not None
            and pressure > 0
            and (
                D(str(d["futures_pressure"])) * sign > 0
                or abs(D(str(d["futures_pressure"]))) <= pressure / 2
            )
            else 0,
        ),
        (
            "leverage_context",
            "OI增加且费率偏反方向（背景项）",
            5,
            5
            if oi is not None
            and funding is not None
            and D(str(oi)) > 0
            and D(str(funding)) * sign < 0
            else 0,
        ),
    ]
    return {
        "version": VERSION,
        "score": sum(p[3] for p in parts)
        if direction in {"LONG", "SHORT"} and d.get("spot_pressure") is not None
        else None,
        "max_score": 100,
        "basis": "original_decision_snapshot_only",
        "components": [
            {"key": k, "label": label, "max": maximum, "points": value}
            for k, label, maximum, value in parts
        ],
        "notice": "规则证据分，不是胜率；OI/费率仅作背景。历史补算不是当时已发布的分数。",
    }


def review_score(signed, risk, entry, order, endpoint_exact, matured, complete):
    """Endpoint direction and structure are deliberately independent."""
    direction = None
    if endpoint_exact and signed is not None and risk > 0:
        endpoint_r = signed / 100 * entry / risk
        direction = round(max(D(0), min(D(100), 50 + 25 * endpoint_r)), 1)
    structure = (
        100
        if order in {"target_first", "target_only"}
        else 0
        if order in {"invalidation_first", "invalidation_only"}
        else 50
        if order == "neither" and complete and matured
        else None
    )
    return {
        "version": "h1-review-score-v1",
        "direction_score": direction,
        "structure_score": structure,
        "provisional": not matured,
        "notice": (
            "方向分=截断(50+25×终点R,0,100)；结构分=先1R 100/先失效0/"
            "到期均未触及50；不确定留空，非收益。"
        ),
    }
