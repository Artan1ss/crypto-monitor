from datetime import UTC, datetime
from decimal import Decimal

from btc_monitor.config import Settings
from btc_monitor.domain import Candle, Direction, Divergence, Pivot, Sweep
from btc_monitor.strategy import SignalEngine, StrategyContext

D = Decimal


def context(spot=True, futures=True):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    candle = Candle(now, D("100"), D("102"), D("98"), D("101"), D("1"), D("200"))
    pivot = Pivot(now, D("100"), 0, "low")
    return StrategyContext(
        symbol="BTCUSDT",
        sweep=Sweep(Direction.LONG, pivot, candle, D("2")),
        atr=D("2"),
        spot_divergence=Divergence(spot, D("1.2") if spot else D("0")),
        futures_divergence=Divergence(futures, D("0.8") if futures else D("0")),
        spot_delta_5m=D("-100"),
        futures_delta_5m=D("-200"),
        price_change_5m=D("-0.4"),
        oi_change_5m=D("-0.5"),
        oi_change_15m=D("-1"),
        funding_rate=D("-0.0002"),
        previous_turnovers=[D("100")] * 20,
        htf_close=D("110"),
        htf_ema50=D("105"),
        htf_ema200=D("100"),
        opposing_pivots=[Pivot(now, D("110"), 1, "high")],
    )


def test_signal_requires_at_least_one_cvd_divergence():
    assert SignalEngine(Settings()).evaluate(context(False, False)) is None


def test_strong_signal_score_and_r_targets():
    signal = SignalEngine(Settings()).evaluate(context())
    assert signal is not None
    assert signal.score == D("8.0")
    assert signal.stop_reference == D("97.6")
    assert signal.tp1 == D("104.4")
    assert signal.tp3 == D("111.2")
    assert signal.structure_target == D("110")
