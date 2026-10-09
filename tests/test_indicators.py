from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btc_monitor.domain import Candle, Direction, Pivot
from btc_monitor.indicators import confirmed_pivots, cvd_divergence, detect_sweep, ema, wilder_atr

D = Decimal


def candle(i, low, high, close=None, confirmed=True):
    return Candle(
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * i),
        D(str(close or low)),
        D(str(high)),
        D(str(low)),
        D(str(close or high)),
        D("1"),
        D("100"),
        confirmed,
    )


def test_wilder_atr_and_ema_have_no_early_values():
    candles = [candle(i, 100 + i, 102 + i, 101 + i) for i in range(20)]
    atr = wilder_atr(candles, 14)
    assert atr[12] is None
    assert atr[13] is not None
    values = ema([D(str(i)) for i in range(1, 11)], 5)
    assert values[3] is None
    assert values[4] == D("3")


def test_pivot_is_confirmed_only_after_right_bars():
    values = [10, 9, 8, 7, 8, 9, 10]
    candles = [candle(i, value, value + 2, value + 1) for i, value in enumerate(values)]
    lows, _ = confirmed_pivots(candles, left=3, right=3)
    assert len(lows) == 1
    assert lows[0].index == 3
    early_lows, _ = confirmed_pivots(candles[:6], left=3, right=3)
    assert early_lows == []


def test_bullish_sweep_requires_reclaim_and_rejection():
    pivot = Pivot(datetime(2026, 1, 1, tzinfo=UTC), D("100"), 0, "low")
    valid = Candle(
        pivot.timestamp + timedelta(hours=1), D("100"), D("102"), D("99"), D("101"), D("1"), D("1")
    )
    assert detect_sweep(valid, pivot, D("2"), Direction.LONG) is not None
    no_reclaim = Candle(valid.timestamp, D("100"), D("101"), D("99"), D("99.5"), D("1"), D("1"))
    assert detect_sweep(no_reclaim, pivot, D("2"), Direction.LONG) is None


def test_cvd_divergence_is_normalized():
    divergence = cvd_divergence(D("110"), D("100"), [D("10")] * 20, Direction.LONG)
    assert divergence.valid
    assert divergence.strength == D("1")
