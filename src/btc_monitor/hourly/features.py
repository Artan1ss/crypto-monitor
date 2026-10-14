from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal

from btc_monitor.archive.exchanges import instruments
from btc_monitor.archive.provider_replay import select_bars
from btc_monitor.archive.replay import eligible, window_features
from btc_monitor.archive.types import serializable

D = Decimal


def universe(source):
    feeds = instruments(("bybit", "binance", "okx", "coinbase"))
    # Fixed provider cohort, NOT a dynamic sum of whatever venues happen to respond.
    return [i for i in feeds if not (source == "coinalyze" and i.key == "okx:spot:BTC-USDT")]


def build_hour(rows, start, decision, source="coinalyze"):
    end = start + timedelta(hours=1)
    expected = universe(source)
    available = eligible(rows, decision, "as_received")
    available.extend(
        r
        for r in rows
        if r["dataset"] == "quality"
        and r["end_time"] > decision
        and r["event_time"] < end
        and r["available_at"] <= decision
    )
    report = window_features(rows, expected, start, end, decision)
    evidence = report["providers"]["coinalyze"] if source == "coinalyze" else report
    feed_reports, input_ids, flows = {}, set(), {}
    metadata = [r for r in available if r["dataset"] == "provider_metadata"]
    input_ids.update(r["id"] for r in metadata)
    for inst in expected:
        per_feed = [
            r
            for r in available
            if (r["venue"], r["market"], r["instrument"]) == (inst.venue, inst.market, inst.symbol)
        ]
        if source == "coinalyze":
            bars, count, resolution = select_bars(per_feed, "provider_flow", start, end)
            complete = evidence["flows"][inst.key]["normalization_complete"]
            total = sum((D(r["payload"]["total_native"]) for r in bars), D(0))
        else:
            # Direct price/flow selection remains identical to archive replay.
            per_feed = [r for r in per_feed if r["source_type"] == "live"]
            gaps = [
                r
                for r in per_feed
                if r["dataset"] == "quality" and r["event_time"] < end and r["end_time"] > start
            ]
            selected = {}
            for r in sorted(per_feed, key=lambda r: (r["available_at"], r["id"])):
                if (
                    r["dataset"] == "flow"
                    and r["interval_seconds"] == 60
                    and r["quality"] == "complete"
                    and start <= r["event_time"] < end
                    and r["end_time"] == r["event_time"] + timedelta(minutes=1)
                    and not any(
                        g["event_time"] < r["end_time"] and g["end_time"] > r["event_time"]
                        for g in gaps
                    )
                ):
                    selected[r["event_time"]] = r
            bars = sorted(selected.values(), key=lambda r: r["event_time"])
            count, resolution = 60, 60
            complete = len(bars) == 60
            total = sum(
                (D(r["payload"]["buy_base"]) + D(r["payload"]["sell_base"]) for r in bars), D(0)
            )
            input_ids.update(r["id"] for r in gaps)
        delta_field = "delta_native" if source == "coinalyze" else "delta_base"
        delta = sum(
            (
                D(r["payload"][delta_field])
                for r in bars
                if r["payload"].get(delta_field) is not None
            ),
            D(0),
        )
        input_ids.update(r["id"] for r in bars)
        price = None
        if len(bars) == count and bars and all(r["payload"].get("close") for r in bars):
            price = {
                "open": D(bars[0]["payload"]["open"]),
                "high": max(D(r["payload"]["high"]) for r in bars),
                "low": min(D(r["payload"]["low"]) for r in bars),
                "close": D(bars[-1]["payload"]["close"]),
            }
        flows[inst.key] = {
            "complete": complete,
            "delta_btc": delta if complete else None,
            "volume_btc": total if complete else None,
            "observed_bars": len(bars),
            "expected_bars": count,
            "resolution_seconds": resolution,
            "price": price,
        }
        if inst.market == "linear":
            d = evidence["derivatives"][inst.key]
            prefix = "provider_" if source == "coinalyze" else ""
            oi = []
            for at in (start, end):
                choices = [
                    r
                    for r in per_feed
                    if r["dataset"] == prefix + "oi"
                    and r["quality"] == "complete"
                    and (r["end_time"] if prefix else r["event_time"]) <= at
                ]
                oi.append(
                    max(
                        choices,
                        key=lambda r: (r["end_time"], r["available_at"], r["id"]),
                        default=None,
                    )
                )
            funding_rows = [
                r
                for r in per_feed
                if r["dataset"] == prefix + "funding"
                and r["quality"] == "complete"
                and r["end_time"] <= end
            ]
            funding = max(
                funding_rows,
                key=lambda r: (r["end_time"], r["available_at"], r["id"]),
                default=None,
            )
            input_ids.update(r["id"] for r in oi + [funding] if r)
            field = "reported_oi_btc" if prefix else "base_oi_btc"
            oi_fresh = d.get("oi_fresh", d.get("oi_complete", False))
            normalized = oi_fresh and all(r and r["payload"].get(field) is not None for r in oi)
            funding_age = d.get("funding_age_seconds")
            funding_rate = (funding or {}).get("payload", {}).get("rate")
            # Keep provider funding separate from interval-normalized exchange funding.
            if prefix:
                funding_rate = (funding or {}).get("payload", {}).get("rate_fraction")
            feed_reports[inst.key] = {
                "oi_change_pct": d["oi_change_pct"] if oi_fresh else None,
                "oi_start_btc": D(oi[0]["payload"][field]) if normalized else None,
                "oi_end_btc": D(oi[1]["payload"][field]) if normalized else None,
                "oi_basis": "provider_unverified" if prefix else "single_side",
                "funding_fraction": D(funding_rate)
                if funding_rate is not None and funding_age is not None and funding_age <= 600
                else None,
                "funding_age_seconds": funding_age,
                "funding_interval_normalized": False,
            }
    aggregates = {}
    for market in ("spot", "linear"):
        selected = [flows[i.key] for i in expected if i.market == market]
        complete = bool(selected) and all(v["complete"] for v in selected)
        aggregates[market] = {
            "complete": complete,
            "delta_btc": sum((v["delta_btc"] for v in selected), D(0)) if complete else None,
            "volume_btc": sum((v["volume_btc"] for v in selected), D(0)) if complete else None,
        }
    price = flows["bybit:linear:BTCUSDT"]["price"]
    all_oi = bool(feed_reports) and all(
        v["oi_start_btc"] is not None and v["oi_end_btc"] is not None for v in feed_reports.values()
    )
    begin = sum((v["oi_start_btc"] for v in feed_reports.values()), D(0)) if all_oi else None
    finish = sum((v["oi_end_btc"] for v in feed_reports.values()), D(0)) if all_oi else None
    rates = [v["funding_fraction"] for v in feed_reports.values()]
    funding = sum(rates, D(0)) / len(rates) if rates and all(v is not None for v in rates) else None
    lineage = sorted((r for r in rows if r["id"] in input_ids), key=lambda r: r["id"])
    digest = hashlib.sha256("\n".join(r["fingerprint"] for r in lineage).encode()).hexdigest()
    return serializable(
        {
            "schema_version": 1,
            "start": start,
            "end": end,
            "decision_at": decision,
            "source": source,
            "causal": True,
            "expected_feeds": [i.key for i in expected],
            "excluded_feeds": ["okx:spot:BTC-USDT"] if source == "coinalyze" else [],
            "complete": price is not None and all(v["complete"] for v in aggregates.values()),
            "price": price,
            "flows": flows,
            "aggregated": aggregates,
            "derivatives": feed_reports,
            "oi_btc": finish,
            "oi_change_pct": (finish - begin) / begin * 100 if begin else None,
            "oi_basis": "same_provider_unverified" if source == "coinalyze" else "single_side",
            "funding_fraction_mean": funding,
            "funding_basis": "unweighted_reported_rate_mean",
            "input_ids": sorted(input_ids),
            "normalization_metadata": [r["payload"] for r in metadata],
            "lineage_sha256": digest,
            "quality_reason": None
            if price and all(v["complete"] for v in aggregates.values())
            else "missing_price_flow_or_verified_units",
        }
    )
