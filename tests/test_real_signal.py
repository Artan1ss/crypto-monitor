"""Mathematical regression from captured aggregates, NOT proof of legacy feed quality."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from btc_monitor.audit import reconstruct
from btc_monitor.diagnostics import cvd_diagnostics
from btc_monitor.domain import Direction


def fixture():
    data = json.loads((Path(__file__).parent / "fixtures" / "signal_20261009.json").read_text())
    rows = [
        SimpleNamespace(
            market=market,
            symbol="BTCUSDT",
            bucket_start=datetime.fromisoformat(time),
            delta_notional=Decimal(delta),
            cumulative_cvd=Decimal(cvd),
            is_complete=None,
        )
        for market, time, delta, cvd in data["rows"]
    ]
    return data, rows


def test_closed_window_real_signal_is_large_accumulation_not_epsilon():
    data, rows = fixture()
    start = datetime.fromisoformat(data["signal_start"])
    pivot = datetime.fromisoformat(data["pivot_start"])
    for row in rows:
        row.is_complete = True  # ONLY for testing the arithmetic window definition.
    _, spot_delta, spot = cvd_diagnostics(
        [r for r in rows if r.market == "spot"], pivot, start, Direction.LONG
    )
    _, futures_delta, futures = cvd_diagnostics(
        [r for r in rows if r.market == "linear"], pivot, start, Direction.LONG
    )
    assert spot["reference_cvd"] == "-5092854.71256054"
    assert spot["current_cvd"] == "-2897612.68751124"
    assert spot["cvd_difference"] == "2195242.02504930"
    assert spot["normalizer_raw"] == "224668.18249660"
    assert Decimal("9.770") < Decimal(spot["strength"]) < Decimal("9.772")
    assert not spot["epsilon_used"]
    assert not spot["continuity_error_minutes"]
    assert spot_delta == Decimal("2453.90924930")
    assert futures_delta == Decimal("-4818461.44030000")
    assert Decimal("0.576") < Decimal(futures["strength"]) < Decimal("0.577")


async def test_legacy_partial_and_pivot_offset_reproduce_both_saved_strengths():
    data, rows = fixture()
    start = datetime.fromisoformat(data["signal_start"])

    async def load(symbol, market, end):
        return [r for r in rows if r.market == market.value and r.bucket_start < end]

    repo = SimpleNamespace(
        load_flow_rows=AsyncMock(side_effect=load),
        tracking_prices=AsyncMock(return_value=([], [])),
        load_candles=AsyncMock(return_value=[]),
    )
    signal = SimpleNamespace(
        id=UUID("15ffcdcd-59b5-462e-9a69-e7a481336f70"),
        symbol="BTCUSDT",
        signal_time=start,
        created_at=start + timedelta(minutes=5, seconds=1),
        reference_timestamp=datetime.fromisoformat(data["pivot_start"]),
        features=data["saved_features"],
        direction="LONG",
        score=Decimal("4.5"),
        strength="MEDIUM",
        entry_reference=Decimal("82294.8"),
        stop_reference=Decimal("82176.3526"),
        tp1=Decimal("82413.2474"),
        tp2=Decimal("82531.6948"),
        tp3=Decimal("82650.1422"),
        reference_price=Decimal("82266.8"),
        reasons=[],
    )
    report = await reconstruct(repo, signal)
    for market in ("spot", "linear"):
        candidate = report["cvd"][market]["legacy_candidate"]
        assert candidate["matches_saved_to_1e_6"]
        corrected = report["cvd"][market]["corrected_closed_window"]
        assert not corrected["capture_completeness_verified"]
        assert corrected["valid"] is None
    assert report["snapshot_origin"] == "historical_reconstruction"
    assert report["parameters_at_original_decision"] is None
