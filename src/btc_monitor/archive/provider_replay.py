"""Coinalyze evidence stays separate from exchange-direct aggregates."""

from datetime import timedelta
from decimal import Decimal


def select_bars(rows, dataset, start, end):
    candidates = [
        r
        for r in rows
        if r["dataset"] == dataset
        and r["quality"] == "complete"
        and start <= r["event_time"] < end
        and r["end_time"] <= end
        and r["payload"].get("provider") == "coinalyze"
    ]
    options = sorted({r["interval_seconds"] for r in candidates if r["interval_seconds"] > 0})
    best, expected, resolution = [], 0, None
    for seconds in options:
        duration = int((end - start).total_seconds())
        if duration % seconds or int(start.timestamp()) % seconds:
            continue
        by_time = {}
        for row in sorted(candidates, key=lambda r: (r["available_at"], r["id"])):
            if row["interval_seconds"] == seconds and row["end_time"] == row[
                "event_time"
            ] + timedelta(seconds=seconds):
                by_time[row["event_time"]] = row
        cursor, selected = start, []
        while cursor < end:
            if cursor in by_time:
                selected.append(by_time[cursor])
            cursor += timedelta(seconds=seconds)
        count = duration // seconds
        if resolution is None or len(selected) / count > len(best) / expected:
            best, expected, resolution = selected, count, seconds
        if len(selected) == count:
            return selected, count, seconds
    return best, expected or int((end - start).total_seconds() / 60), resolution


def provider_features(available, expected, start, end):
    flows, derivatives = {}, {}
    for inst in expected:
        rows = [
            r
            for r in available
            if (r["venue"], r["market"], r["instrument"]) == (inst.venue, inst.market, inst.symbol)
            and r["payload"].get("provider") == "coinalyze"
        ]
        bars, count, resolution = select_bars(rows, "provider_flow", start, end)
        units = {r["payload"]["volume_unit"] for r in bars}
        complete = (
            len(bars) == count
            and bool(bars)
            and all(r["payload"].get("delta_native") is not None for r in bars)
            and len(units) == 1
        )
        native = sum(
            (
                Decimal(r["payload"]["delta_native"])
                for r in bars
                if r["payload"].get("delta_native") is not None
            ),
            Decimal(0),
        )
        normalized = complete and units == {"base"}
        flows[inst.key] = {
            "complete": complete,
            "normalization_complete": normalized,
            "interval_seconds": resolution,
            "expected_bars": count,
            "observed_bars": len(bars),
            "volume_unit": next(iter(units)) if len(units) == 1 else "unknown_or_mixed",
            "delta_native": native if complete else None,
            "delta_btc": native if normalized else None,
            "observation_ids_sample": [r["id"] for r in bars[:8]],
            "price": {
                "open": Decimal(bars[0]["payload"]["open"]),
                "high": max(Decimal(r["payload"]["high"]) for r in bars),
                "low": min(Decimal(r["payload"]["low"]) for r in bars),
                "close": Decimal(bars[-1]["payload"]["close"]),
                "quote_currency": inst.quote,
            }
            if bars and len(bars) == count
            else None,
        }
        if inst.market != "linear":
            continue

        def latest(dataset, at, series_rows=rows):
            return max(
                (
                    r
                    for r in series_rows
                    if r["dataset"] == dataset
                    and r["end_time"] <= at
                    and r["quality"] == "complete"
                ),
                key=lambda r: (r["end_time"], r["available_at"], r["id"]),
                default=None,
            )

        first, last = latest("provider_oi", start), latest("provider_oi", end)
        fresh = (
            first is not None
            and last is not None
            and (
                start - first["end_time"] <= timedelta(minutes=10)
                and end - last["end_time"] <= timedelta(minutes=10)
                and first["payload"]["volume_unit"] == last["payload"]["volume_unit"]
                and first["payload"]["provider_symbol"] == last["payload"]["provider_symbol"]
            )
        )
        begin = Decimal(first["payload"]["reported_oi_native"]) if fresh else None
        finish = Decimal(last["payload"]["reported_oi_native"]) if fresh else None
        funding = latest("provider_funding", end)
        liquidations, liquidation_count, _ = select_bars(rows, "provider_liquidations", start, end)
        liquidation_complete = bool(liquidations) and len(liquidations) == liquidation_count
        liquidation_normalized = liquidation_complete and all(
            r["payload"].get("short_btc") is not None for r in liquidations
        )
        known_liquidations = [r for r in liquidations if r["payload"].get("short_btc") is not None]
        derivatives[inst.key] = {
            "oi_fresh": fresh,
            "oi_change_pct": (finish - begin) / begin * 100 if fresh and begin > 0 else None,
            "oi_side_basis": "provider_basis_unverified",
            "oi_observation_ids": [r["id"] for r in (first, last) if r],
            "funding": funding["payload"] if funding else None,
            "funding_age_seconds": (end - funding["end_time"]).total_seconds() if funding else None,
            "liquidation_coverage_complete": liquidation_complete,
            "observed_short_liquidations_btc": sum(
                (Decimal(r["payload"]["short_btc"]) for r in known_liquidations),
                Decimal(0),
            )
            if known_liquidations
            else None,
            "short_liquidations_btc": sum(
                (Decimal(r["payload"]["short_btc"]) for r in liquidations), Decimal(0)
            )
            if liquidation_normalized
            else None,
        }
    aggregates = {}
    for market in ("spot", "linear"):
        keys = [i.key for i in expected if i.market == market]
        complete_keys = [k for k in keys if flows[k]["normalization_complete"]]
        complete = bool(keys) and len(complete_keys) == len(keys)
        observed = sum((flows[k]["delta_btc"] for k in complete_keys), Decimal(0))
        aggregates[market] = {
            "complete": complete,
            "expected_feeds": keys,
            "complete_feeds": complete_keys,
            "delta_btc": observed if complete else None,
            "observed_delta_btc": observed,
        }
    return {
        "provider": "coinalyze",
        "flows": flows,
        "derivatives": derivatives,
        "aggregated": aggregates,
        "notice": (
            "Separate provider evidence; never added to direct exchange flows. "
            "Sparse liquidation bars are not assumed zero."
        ),
    }
