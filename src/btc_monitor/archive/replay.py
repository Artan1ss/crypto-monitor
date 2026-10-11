from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal

from btc_monitor.archive.provider_replay import provider_features
from btc_monitor.archive.types import Instrument, utc

TIMEFRAMES = {"5m": 300, "1h": 3600, "4h": 14400, "1d": 86400}


def eligible(rows: list[dict], decision_at: datetime, mode: str) -> list[dict]:
    if mode not in {"as_received", "historical"}:
        raise ValueError("Unknown replay mode")
    return [
        r
        for r in rows
        if r["end_time"] <= decision_at
        and (mode == "historical" or r["available_at"] <= decision_at)
    ]


def latest(rows: list[dict], dataset: str, inst: Instrument, at: datetime):
    candidates = [
        r
        for r in rows
        if r["dataset"] == dataset
        and (r["venue"], r["market"], r["instrument"]) == (inst.venue, inst.market, inst.symbol)
        and r["event_time"] <= at
        and r["quality"] == "complete"
    ]
    return max(
        candidates, key=lambda r: (r["event_time"], r["available_at"], r["id"]), default=None
    )


def flow_window(rows: list[dict], inst: Instrument, start: datetime, end: datetime):
    candidates = [
        r
        for r in rows
        if (r["venue"], r["market"], r["instrument"]) == (inst.venue, inst.market, inst.symbol)
    ]
    gaps = [
        r
        for r in candidates
        if r["dataset"] == "quality" and r["event_time"] < end and r["end_time"] > start
    ]
    by_minute: dict[datetime, list[dict]] = {}
    for row in candidates:
        if (
            row["dataset"] == "flow"
            and row["interval_seconds"] == 60
            and start <= row["event_time"] < end
            and row["end_time"] <= end
        ):
            by_minute.setdefault(row["event_time"], []).append(row)
    selected, reasons = [], {}
    cursor = start
    while cursor < end:
        options = by_minute.get(cursor, [])
        valid = [
            r
            for r in options
            if r["quality"] == "complete"
            and not (
                r["source_type"] == "live"
                and any(g["event_time"] < r["end_time"] and g["end_time"] > cursor for g in gaps)
            )
        ]
        chosen = max(
            valid,
            key=lambda r: (r["source_type"] == "live", r["available_at"], r["id"]),
            default=None,
        )
        if chosen:
            selected.append(chosen)
        else:
            reason = "missing_or_partial_minute"
            reasons[reason] = reasons.get(reason, 0) + 1
        cursor += timedelta(minutes=1)
    expected = int((end - start).total_seconds() / 60)
    total = sum((Decimal(r["payload"]["delta_base"]) for r in selected), Decimal(0))
    digest = hashlib.sha256("\n".join(r["fingerprint"] for r in selected).encode()).hexdigest()
    return {
        "complete": len(selected) == expected,
        "expected_minutes": expected,
        "complete_minutes": len(selected),
        "observed_delta_btc": total,
        "delta_btc": total if len(selected) == expected else None,
        "buy_btc": sum((Decimal(r["payload"]["buy_base"]) for r in selected), Decimal(0)),
        "sell_btc": sum((Decimal(r["payload"]["sell_base"]) for r in selected), Decimal(0)),
        "quote_currency": inst.quote,
        "gap_reasons": reasons,
        "lineage_sha256": digest,
        "observation_ids_sample": [r["id"] for r in selected[:8]],
    }


def window_features(
    rows: list[dict],
    expected: list[Instrument],
    start: datetime,
    end: datetime,
    decision_at: datetime,
    mode="as_received",
) -> dict:
    start, end, decision_at = utc(start), utc(end), utc(decision_at)
    if (
        start >= end
        or end > decision_at
        or not expected
        or len({i.key for i in expected}) != len(expected)
        or any(t.second or t.microsecond for t in (start, end))
    ):
        raise ValueError("Invalid replay window or expected universe")
    available = eligible(rows, decision_at, mode)
    # A quality gap overlapping the window can end AFTER decision_at. Its reception is
    # causal evidence already known; do not accidentally filter it out by end_time.
    available.extend(
        r
        for r in rows
        if r["dataset"] == "quality"
        and r["end_time"] > decision_at
        and r["event_time"] < end
        and (mode == "historical" or r["available_at"] <= decision_at)
    )
    flows = {inst.key: flow_window(available, inst, start, end) for inst in expected}
    aggregates = {}
    for market in ("spot", "linear"):
        keys = [i.key for i in expected if i.market == market]
        complete = bool(keys) and all(flows[k]["complete"] for k in keys)
        aggregates[market] = {
            "complete": complete,
            "expected_venues": [k.split(":")[0] for k in keys],
            "complete_venues": [k.split(":")[0] for k in keys if flows[k]["complete"]],
            "delta_btc": sum((flows[k]["delta_btc"] for k in keys), Decimal(0))
            if complete
            else None,
            "positive_venues": [
                k.split(":")[0] for k in keys if flows[k]["complete"] and flows[k]["delta_btc"] > 0
            ],
            "negative_venues": [
                k.split(":")[0] for k in keys if flows[k]["complete"] and flows[k]["delta_btc"] < 0
            ],
        }
    derivatives = {}
    for inst in expected:
        if inst.market != "linear":
            continue
        oi_start, oi_end = latest(available, "oi", inst, start), latest(available, "oi", inst, end)
        funding = latest(available, "funding", inst, end)
        fresh = (
            oi_start is not None
            and oi_end is not None
            and (
                start - oi_start["event_time"] <= timedelta(minutes=10)
                and end - oi_end["event_time"] <= timedelta(minutes=10)
            )
        )
        begin = Decimal(oi_start["payload"]["base_oi_btc"]) if fresh else None
        finish = Decimal(oi_end["payload"]["base_oi_btc"]) if fresh else None
        derivatives[inst.key] = {
            "oi_complete": fresh,
            "oi_base_btc_start": begin,
            "oi_base_btc_end": finish,
            "oi_change_pct": (finish - begin) / begin * 100 if fresh and begin > 0 else None,
            "oi_observation_ids": [r["id"] for r in (oi_start, oi_end) if r],
            "funding": funding["payload"] if funding else None,
            "funding_event_time": funding["event_time"] if funding else None,
            "funding_age_seconds": (end - funding["event_time"]).total_seconds()
            if funding
            else None,
        }
    price_inst = next(
        (i for i in expected if i.venue == "bybit" and i.market == "linear"), expected[0]
    )
    price_rows = [
        r
        for r in available
        if (r["venue"], r["market"], r["instrument"])
        == (price_inst.venue, price_inst.market, price_inst.symbol)
        and r["dataset"] in {"flow", "candle"}
        and r["interval_seconds"] == 60
        and r["end_time"] == r["event_time"] + timedelta(minutes=1)
        and r["quality"] == "complete"
        and start <= r["event_time"] < end
        and r["end_time"] <= end
        and r["payload"].get("close") is not None
        and not (
            r["source_type"] == "live"
            and any(
                g["dataset"] == "quality"
                and (g["venue"], g["market"], g["instrument"])
                == (price_inst.venue, price_inst.market, price_inst.symbol)
                and g["event_time"] < r["end_time"]
                and g["end_time"] > r["event_time"]
                for g in available
            )
        )
    ]
    price_map = {}
    for r in sorted(
        price_rows, key=lambda r: (r["source_type"] == "live", r["available_at"], r["id"])
    ):
        price_map[r["event_time"]] = r
    candles = sorted(price_map.values(), key=lambda r: r["event_time"])
    expected_minutes = int((end - start).total_seconds() / 60)
    price = None
    if len(candles) == expected_minutes:
        price = {
            "venue": price_inst.venue,
            "instrument": price_inst.symbol,
            "open": Decimal(candles[0]["payload"]["open"]),
            "high": max(Decimal(r["payload"]["high"]) for r in candles),
            "low": min(Decimal(r["payload"]["low"]) for r in candles),
            "close": Decimal(candles[-1]["payload"]["close"]),
        }
    valid_flow = all(a["complete"] for a in aggregates.values())
    return {
        "schema_version": 1,
        "window_start": start,
        "window_end": end,
        "decision_at": decision_at,
        "replay_mode": mode,
        "causal": mode == "as_received",
        "flow_complete": valid_flow,
        "flows": flows,
        "aggregated": aggregates,
        "derivatives": derivatives,
        "providers": {"coinalyze": provider_features(available, expected, start, end)},
        "price": price,
        "spot_buy_futures_sell": valid_flow
        and aggregates["spot"]["delta_btc"] > 0
        and aggregates["linear"]["delta_btc"] < 0,
        "notice": "Research features only; not a squeeze prediction or trade backtest.",
    }


def replay_windows(rows, expected, start, end, timeframe, mode, decision_delay_seconds=15):
    seconds = TIMEFRAMES[timeframe]
    if int(start.timestamp()) % seconds or int(end.timestamp()) % seconds:
        raise ValueError("Replay range must align to the selected UTC timeframe")
    cursor = start
    while cursor + timedelta(seconds=seconds) <= end:
        finish = cursor + timedelta(seconds=seconds)
        yield window_features(
            rows, expected, cursor, finish, finish + timedelta(seconds=decision_delay_seconds), mode
        )
        cursor = finish
