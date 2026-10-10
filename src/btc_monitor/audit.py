"""Reconstruct legacy diagnostics without claiming they were captured at decision time.

Usage: python -m btc_monitor.audit [--persist]
The default is read-only. --persist fills ONLY missing historical debug snapshots
and initial outcomes; it never changes signal decisions or sends alerts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from types import SimpleNamespace

from sqlalchemy import select

from btc_monitor.config import get_settings
from btc_monitor.db import Database
from btc_monitor.diagnostics import cvd_diagnostics, flow_row_snapshot, json_value
from btc_monitor.domain import Direction, Market
from btc_monitor.models import SignalRecord
from btc_monitor.repository import Repository
from btc_monitor.tracking import OutcomeTracker


async def reconstruct(repository, signal):
    end = signal.signal_time + timedelta(minutes=5)
    market_reports, raw = {}, {}
    for market, prefix in ((Market.SPOT, "spot"), (Market.LINEAR, "futures")):
        rows = await repository.load_flow_rows(
            signal.symbol, market, end=end + timedelta(minutes=1)
        )
        raw[market.value] = [flow_row_snapshot(r) for r in rows]
        # Mathematical reconstruction only: legacy capture completeness is UNKNOWN.
        mathematical_rows = [
            SimpleNamespace(
                **{
                    "bucket_start": r.bucket_start,
                    "delta_notional": r.delta_notional,
                    "cumulative_cvd": r.cumulative_cvd,
                    "is_complete": True,
                }
            )
            for r in rows
        ]
        _, _, corrected = cvd_diagnostics(
            mathematical_rows,
            signal.reference_timestamp,
            signal.signal_time,
            Direction(signal.direction),
        )
        corrected["capture_completeness_verified"] = all(
            r.is_complete is True
            for r in rows
            if signal.reference_timestamp <= r.bucket_start < end
        )
        corrected["valid"] = None
        corrected["data_valid"] = None
        saved_strength = Decimal(signal.features[f"{prefix}_cvd_strength"])
        saved_delta = Decimal(signal.features[f"{prefix}_delta_5m"])
        by_time = {r.bucket_start: r for r in rows}
        old_ref = by_time.get(signal.reference_timestamp + timedelta(minutes=5))
        close_row = by_time.get(end - timedelta(minutes=1))
        # Candidate reconstruction of the next-minute partial delta using saved last-5-row sum.
        last_four = [by_time.get(end - timedelta(minutes=i)) for i in range(1, 5)]
        partial = saved_delta - sum((r.delta_notional for r in last_four if r), Decimal(0))
        grouped = {}
        for r in rows:
            if r.bucket_start >= end:
                continue
            bucket = r.bucket_start.replace(minute=(r.bucket_start.minute // 5) * 5)
            grouped[bucket] = grouped.get(bucket, Decimal(0)) + r.delta_notional
        grouped[end] = partial
        baseline = [grouped[t] for t in sorted(grouped)][-20:]
        legacy_normalizer = Decimal(str(median([abs(x) for x in baseline])))
        candidate_difference = (
            close_row.cumulative_cvd + partial - old_ref.cumulative_cvd
            if (close_row and old_ref)
            else None
        )
        candidate_strength = (
            (candidate_difference if signal.direction == "LONG" else -candidate_difference)
            / legacy_normalizer
            if (candidate_difference is not None and legacy_normalizer)
            else None
        )
        market_reports[market.value] = json_value(
            {
                "saved_strength": saved_strength,
                "saved_delta_5m": saved_delta,
                "corrected_closed_window": corrected,
                "legacy_candidate": {
                    "assumption": (
                        "last five rows were four closed minutes plus next-minute partial"
                    ),
                    "next_minute_partial_inferred": partial,
                    "reference_minute_inclusive_bug": old_ref.bucket_start if old_ref else None,
                    "reference_cvd_inclusive_bug": old_ref.cumulative_cvd if old_ref else None,
                    "normalizer_including_signal_and_partial": legacy_normalizer,
                    "directional_numerator": candidate_difference
                    if signal.direction == "LONG"
                    else -candidate_difference
                    if candidate_difference is not None
                    else None,
                    "reconstructed_strength": candidate_strength,
                    "matches_saved_to_1e_6": abs(candidate_strength - saved_strength)
                    < Decimal("0.000001")
                    if candidate_strength is not None
                    else False,
                    "not_exact_live_evidence": True,
                },
            }
        )
    candles, ticks = await repository.tracking_prices(
        signal.symbol, signal.reference_timestamp - timedelta(minutes=100), end
    )
    candle_history = await repository.load_candles(signal.symbol, "5", 300, end=end)
    hourly_history = await repository.load_candles(signal.symbol, "60", 300, end=end)
    return json_value(
        {
            "schema_version": 1,
            "snapshot_origin": "historical_reconstruction",
            "reconstructed_at": datetime.now(UTC),
            "signal_id": signal.id,
            "generated_at": signal.created_at,
            "candle_start": signal.signal_time,
            "candle_close": end,
            "reference_timestamp": signal.reference_timestamp,
            "legacy_features": signal.features,
            "legacy_score": signal.score,
            "parameters_at_original_decision": None,
            "limitations": [
                "Original live snapshot and next-minute partial trade state were not saved.",
                "Legacy minute capture completeness and original parameters are unknown.",
                "Reconstructed diagnostics do not change historical signals.",
            ],
            "cvd": market_reports,
            "raw_flow_rows": raw,
            "candles_1h": hourly_history,
            "decision": {
                k: getattr(signal, k)
                for k in (
                    "id",
                    "direction",
                    "score",
                    "strength",
                    "entry_reference",
                    "stop_reference",
                    "tp1",
                    "tp2",
                    "tp3",
                    "reference_price",
                    "reference_timestamp",
                    "reasons",
                )
            },
            "candles_5m": [
                {
                    k: getattr(c, k)
                    for k in ("timestamp", "open", "high", "low", "close", "turnover", "atr14")
                }
                for c in candle_history
            ],
            "ticker_inputs": [
                {
                    k: getattr(t, k)
                    for k in ("timestamp", "last_price", "open_interest", "funding_rate")
                }
                for t in ticks
            ],
        }
    )


async def run(persist=False, refresh_reconstruction=False):
    settings = get_settings()
    database = Database(settings.database_url)
    repository = Repository(database)
    reports = []
    try:
        async with database.session() as session:
            signals = list(
                (
                    await session.scalars(select(SignalRecord).order_by(SignalRecord.created_at))
                ).all()
            )
        for signal in signals:
            if signal.debug_snapshot is not None and not (
                refresh_reconstruction
                and signal.debug_snapshot.get("snapshot_origin") == "historical_reconstruction"
            ):
                continue
            report = await reconstruct(repository, signal)
            reports.append(
                {
                    "id": str(signal.id),
                    "signal_time": signal.signal_time.isoformat(),
                    "cvd": report["cvd"],
                }
            )
            if persist:
                async with database.session() as session:
                    row = await session.get(SignalRecord, signal.id)
                    if row.debug_snapshot is None or (
                        refresh_reconstruction
                        and row.debug_snapshot.get("snapshot_origin") == "historical_reconstruction"
                    ):
                        row.debug_snapshot = report
        if persist:
            await OutcomeTracker(repository, settings).update(datetime.now(UTC))
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--refresh-reconstruction", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.persist, args.refresh_reconstruction))
