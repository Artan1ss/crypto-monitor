"""Frozen V2 shape rules shared by historical replay and causal runtime."""
from collections import Counter
from datetime import datetime, timedelta
from btc_monitor.archive.types import serializable
from btc_monitor.hourly.sweep import D

VERSION = "squeeze-15m-morphology-v2"
PARAMETERS = {
    "version": VERSION, "interval_seconds": 900, "window_bars": 12,
    "minimum_spot_pressure": "0.01", "minimum_opposing_futures_pressure": "0.01",
    "minimum_oi_growth": "0.003", "minimum_positive_oi_steps": 8,
    "minimum_spot_venues": 2, "minimum_growing_oi_venues": 2,
    "relative_minimum_pressure_gap": "0.01",
    "relative_maximum_opposing_spot_pressure": "0.03",
    "relative_maximum_spot_to_futures_ratio": "0.5",
    "minimum_price_move_atr": "-0.75", "maximum_price_move_atr": "1.5",
    "episode_expiry_bars": 16, "rearm_false_bars": 4,
    "release_breakout_atr": "0.1", "release_1h_oi_drop": "-0.002",
    "scope": "shape_rules_only_delivery_authority_belongs_to_runtime",
    "score_basis": "fixed evidence rubric, not a calibrated squeeze probability",
}
FEEDS = ["bybit:spot:BTCUSDT", "binance:spot:BTCUSDT", "coinbase:spot:BTC-USD",
         "bybit:linear:BTCUSDT", "binance:linear:BTCUSDT", "okx:linear:BTC-USDT-SWAP"]
FUTURES = [f for f in FEEDS if ":linear:" in f]


def finite(value):
    try:
        return D(value).is_finite()
    except Exception:
        return False


def build_bars(rows, manifest):
    indexed = {}
    for index, row in enumerate(rows):
        feed = f"{row.venue}:{row.market}:{row.instrument}"
        key = (row.event_time, feed, row.dataset)
        if key in indexed:
            raise ValueError("duplicate_input_series_timestamp")
        indexed[key] = (row, index)
    result = []
    start, end = map(datetime.fromisoformat, (manifest["requested_start"], manifest["requested_end"]))
    while start < end:
        item = {"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(),
            "closed": True, "complete": True, "flow": {}, "oi": {}, "funding": {}, "input_ids": [], "quality": []}
        for feed in FEEDS:
            for dataset in ["provider_flow"] + (["provider_oi", "provider_funding"] if feed in FUTURES else []):
                record = indexed.get((start, feed, dataset))
                if record is None:
                    if dataset != "provider_funding":
                        item["complete"] = False
                        item["quality"].append(f"missing:{feed}:{dataset}")
                    continue
                row, index = record
                p = row.payload
                valid = row.interval_seconds == 900 and row.end_time == start + timedelta(minutes=15) and row.quality == "complete" and p.get("volume_unit") == "base"
                if dataset == "provider_flow":
                    v, delta = p.get("total_native"), p.get("delta_btc")
                    valid = valid and finite(v) and finite(delta) and D(v) > 0 and abs(D(delta)) <= D(v)
                    valid = valid and all(finite(p.get(k)) for k in ("open", "high", "low", "close"))
                    valid = valid and D(0) < D(p["low"]) <= min(D(p["open"]), D(p["close"])) <= max(D(p["open"]), D(p["close"])) <= D(p["high"])
                    if valid:
                        item["flow"][feed] = {"volume": str(v), "delta": str(delta)}
                    if feed == "bybit:linear:BTCUSDT" and valid:
                        item["price"] = {k: str(p[k]) for k in ("open", "high", "low", "close")}
                elif dataset == "provider_oi":
                    valid = valid and finite(p.get("reported_oi_btc")) and D(p["reported_oi_btc"]) > 0
                    if valid:
                        item["oi"][feed] = str(p["reported_oi_btc"])
                else:
                    if valid and finite(p.get("rate_fraction")):
                        item["funding"][feed] = str(p["rate_fraction"])
                        item["input_ids"].append(index)
                    continue
                if not valid:
                    item["complete"] = False
                    item["quality"].append(f"invalid:{feed}:{dataset}")
                item["input_ids"].append(index)
        if "price" not in item:
            item["complete"] = False
        if item["complete"]:
            for market in ("spot", "linear"):
                item[market] = {k: str(sum((D(v[k]) for f, v in item["flow"].items() if f":{market}:" in f), D(0))) for k in ("delta", "volume")}
            item["oi_total"] = str(sum(map(D, item["oi"].values()), D(0)))
        result.append(item)
        start += timedelta(minutes=15)
    return result


def features(history, direction):
    b = history[-15:]
    if len(b) < 15 or any(not r.get("complete") or not r.get("closed") for r in b):
        return None
    if any(datetime.fromisoformat(a["end"]) != datetime.fromisoformat(c["start"]) for a, c in zip(b, b[1:])):
        return None
    sign = 1 if direction == "SHORT_SQUEEZE" else -1
    w, prev = b[-12:], b[-13]
    atr = sum((max(D(c["price"]["high"])-D(c["price"]["low"]), abs(D(c["price"]["high"])-D(a["price"]["close"])), abs(D(c["price"]["low"])-D(a["price"]["close"]))) for a, c in zip(b[:-1], b[1:])), D(0)) / 14
    if atr <= 0:
        return None
    f = {"atr": atr, "sign": sign, "window_start": w[0]["start"], "window_end": w[-1]["end"]}
    for market in ("spot", "linear"):
        delta = sum((D(r[market]["delta"]) for r in w), D(0))
        volume = sum((D(r[market]["volume"]) for r in w), D(0))
        f[market] = {"delta": delta, "normalizer": volume, "pressure": delta/volume}
    oi = [D(r["oi_total"]) for r in b[-13:]]
    f.update(oi_growth=oi[-1]/oi[0]-1, oi_1h=oi[-1]/oi[-5]-1,
        positive_oi_steps=sum(y > x for x, y in zip(oi, oi[1:])),
        oi_venue_growth={feed: D(w[-1]["oi"][feed])/D(prev["oi"][feed])-1 for feed in FUTURES},
        spot_venue_deltas={feed: sum((D(r["flow"][feed]["delta"]) for r in w), D(0)) for feed in FEEDS if ":spot:" in feed},
        spot_venue_pressures={feed: sum((D(r["flow"][feed]["delta"]) for r in w), D(0)) / sum((D(r["flow"][feed]["volume"]) for r in w), D(0)) for feed in FEEDS if ":spot:" in feed},
        price_move_atr=sign*(D(w[-1]["price"]["close"])-D(prev["price"]["close"]))/atr,
        ceiling=max(D(r["price"]["high"]) for r in w), floor=min(D(r["price"]["low"]) for r in w))
    funding = w[-1]["funding"]
    f["funding_mean"] = sum(map(D, funding.values()), D(0))/len(funding) if len(funding)==3 else None
    f["signed_pressure_gap"] = sign * (f["spot"]["pressure"] - f["linear"]["pressure"])
    f["opposing_spot_to_futures_ratio"] = max(D(0), -sign*f["spot"]["pressure"]) / (-sign*f["linear"]["pressure"]) if sign*f["linear"]["pressure"] < 0 else None
    f["divergence_type"] = divergence_type(f)
    return f


def divergence_type(f):
    """Relative pressure is compared in turnover fractions, never raw BTC slopes."""
    sign, p = f["sign"], PARAMETERS
    spot, futures = sign*f["spot"]["pressure"], sign*f["linear"]["pressure"]
    if (spot >= D(p["minimum_spot_pressure"]) and
        sum(v*sign>0 for v in f["spot_venue_deltas"].values()) >= p["minimum_spot_venues"]):
        return "opposite_direction"
    if (futures <= -D(p["minimum_opposing_futures_pressure"]) and
        spot >= -D(p["relative_maximum_opposing_spot_pressure"]) and
        spot-futures >= D(p["relative_minimum_pressure_gap"]) and
        max(D(0), -spot) <= -futures*D(p["relative_maximum_spot_to_futures_ratio"]) and
        sum(v*sign >= futures*D(p["relative_maximum_spot_to_futures_ratio"])
            for v in f["spot_venue_pressures"].values()) >= p["minimum_spot_venues"]):
        return "relative_resilience"
    return None


def shape(f):
    if f is None:
        return ["missing_or_discontinuous_15m_inputs"]
    p, sign = PARAMETERS, f["sign"]
    checks = {
        "spot_relative_or_opposite_divergence_missing": f["divergence_type"] is not None,
        "futures_flow_not_opposed": f["linear"]["pressure"]*sign <= -D(p["minimum_opposing_futures_pressure"]),
        "oi_growth_too_small": f["oi_growth"] >= D(p["minimum_oi_growth"]),
        "oi_not_persistent": f["positive_oi_steps"] >= p["minimum_positive_oi_steps"] and f["oi_1h"] > 0,
        "oi_venue_breadth_insufficient": sum(v>0 for v in f["oi_venue_growth"].values()) >= p["minimum_growing_oi_venues"],
        "price_not_absorbing_or_already_expanded": D(p["minimum_price_move_atr"]) <= f["price_move_atr"] <= D(p["maximum_price_move_atr"]),
    }
    return [k for k, passed in checks.items() if not passed]


def score(f):
    sign = f["sign"]
    parts = {"现货主动方向": round(25*min(D(1),f["spot"]["pressure"]*sign/D(".03"))),
        "合约反向主动成交": round(20*min(D(1),-f["linear"]["pressure"]*sign/D(".03"))),
        "OI累积强度": round(20*min(D(1),f["oi_growth"]/D(".015"))),
        "OI持续性": round(15*D(f["positive_oi_steps"])/12),
        "现货多源一致": round(10*sum(v*sign>0 for v in f["spot_venue_deltas"].values())/3),
        "价格吸收": 5 if abs(f["price_move_atr"]) <= D(".75") else 2,
        "费率背景": 5 if f["funding_mean"] is not None and f["funding_mean"]*sign<0 else 0}
    if f["divergence_type"] == "relative_resilience":
        # Relative resilience is weaker evidence than independent opposing spot flow.
        parts.pop("现货主动方向")
        parts.pop("现货多源一致")
        parts["现货相对抗压（上限15）"] = round(15*min(D(1),f["signed_pressure_gap"]/D(".03")))
        futures = f["linear"]["pressure"]*sign
        breadth = sum(v*sign >= futures*D(PARAMETERS["relative_maximum_spot_to_futures_ratio"])
                      for v in f["spot_venue_pressures"].values())
        parts["现货相对分化多源一致"] = round(10*breadth/3)
    parts = {key: max(0, value) for key, value in parts.items()}
    return {"score": sum(parts.values()), "max": 100, "components": parts, "not_probability": True,
        "divergence_type": f["divergence_type"], "evidence_ceiling": 90 if f["divergence_type"] == "relative_resilience" else 100,
        "funding_missing": f["funding_mean"] is None}


def annotate(bars, state=None):
    # Persist this bounded state atomically with its alert outbox. Historical
    # replay and restart-safe streaming must produce the same shape sequence.
    state = state if state is not None else {}
    signals, audit = [], Counter()
    episodes = state.setdefault("episodes", {})
    history = state.setdefault("history", [])
    i = state.get("index", -1)
    for bar in bars:
        i += 1
        history = (history + [bar])[-15:]
        for direction in ("SHORT_SQUEEZE", "LONG_SQUEEZE"):
            f = features(history, direction)
            rejected = shape(f)
            episode = episodes.get(direction)
            if episode:
                episode["false_bars"] = episode["false_bars"]+1 if rejected else 0
                if f is None:
                    episodes.pop(direction)
                    continue
                if not episode["released"] and i-episode["index"] <= PARAMETERS["episode_expiry_bars"]:
                    sign = f["sign"]
                    buffer = D(episode["atr"]) * D(PARAMETERS["release_breakout_atr"])
                    crossed = D(bar["price"]["close"]) > D(episode["ceiling"])+buffer if sign>0 else D(bar["price"]["close"]) < D(episode["floor"])-buffer
                    hour_delta = sum((D(r["linear"]["delta"]) for r in history[-4:]),D(0))
                    if crossed and f["oi_1h"] <= D(PARAMETERS["release_1h_oi_drop"]) and hour_delta*sign>0:
                        signals.append({"id": f"{direction}:{bar['start']}:release", "direction": direction,
                            "phase": "RELEASE", "bar_start": bar["start"], "evaluated_at": bar["end"],
                            "price_reference": bar["price"]["close"], "parent_id": episode["id"],
                            "confidence": episode["confidence"], "score_basis": "original_watch_evidence_not_release_probability",
                            "divergence_type": episode["divergence_type"],
                            "diagnostics": serializable(f), "reasons": ["越过预警时固定区间边界", "OI回落且合约主动成交转向；仅去杠杆迹象，不证明清算"],
                            "snapshot": serializable({"history": history[:], "parameters": PARAMETERS})})
                        episode["released"] = True
                if i-episode["index"] > PARAMETERS["episode_expiry_bars"] and episode["false_bars"] >= PARAMETERS["rearm_false_bars"]:
                    episodes.pop(direction)
                else:
                    continue
            if rejected:
                audit.update(rejected)
                continue
            evidence = score(f)
            signal = {"id": f"{direction}:{bar['start']}:watch", "direction": direction, "phase": "WATCH",
                "bar_start": bar["start"], "evaluated_at": bar["end"], "price_reference": bar["price"]["close"],
                "confidence": evidence, "score_basis": "closed_prefix_only_fixed_rubric",
                "divergence_type": f["divergence_type"],
                "diagnostics": serializable(f), "reasons": ["现货/合约主动成交持续反向" if f["divergence_type"] == "opposite_direction" else "现货相对抗压：即使同向，归一化压力明显弱于合约", "同源OI上升且多数15m步长增加", "价格未明显沿合约成交方向扩张"],
                "snapshot": serializable({"history": history[:], "parameters": PARAMETERS})}
            signals.append(signal)
            episodes[direction] = {"id": signal["id"], "index": i, "false_bars": 0, "released": False,
                "ceiling": f["ceiling"], "floor": f["floor"], "atr": f["atr"], "confidence": evidence, "divergence_type": f["divergence_type"]}
    state.update(index=i, history=serializable(history), episodes=serializable(episodes))
    return signals, dict(audit)
