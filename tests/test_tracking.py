from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from btc_monitor.tracking import OutcomeTracker, track_signal

D = Decimal
START = datetime(2026, 1, 1, 12, tzinfo=UTC)


def signal(direction="LONG", start=START):
    return SimpleNamespace(
        created_at=start,
        direction=direction,
        entry_reference=D(100),
        stop_reference=D(90) if direction == "LONG" else D(110),
        tp1=D(110) if direction == "LONG" else D(90),
        tp2=D(120) if direction == "LONG" else D(80),
        tp3=D(130) if direction == "LONG" else D(70),
        telegram_sent_at=None,
    )


def candle(minutes, low, high, close=100):
    return SimpleNamespace(
        timestamp=START + timedelta(minutes=minutes), low=D(low), high=D(high), close=D(close)
    )


def tick(seconds, price):
    return SimpleNamespace(timestamp=START + timedelta(seconds=seconds), last_price=D(price))


def test_same_bar_target_and_stop_are_ambiguous_not_a_win():
    result = track_signal(signal(), [candle(0, 85, 125)], [], START + timedelta(minutes=15))
    window = result["windows"]["15"]
    assert window["target_stop_order"]["tp1"] == "ambiguous"
    assert window["ambiguous"]
    assert window["coverage"] == "incomplete"


def test_signal_bar_is_excluded_when_generated_after_open():
    result = track_signal(
        signal(start=START + timedelta(seconds=1)),
        [candle(0, 1, 999)],
        [tick(2, 101)],
        START + timedelta(minutes=15, seconds=1),
    )
    window = result["windows"]["15"]
    assert window["mfe_r"] == "0.1"
    assert window["mae_r"] == "0"
    assert not window["first_touches"]
    assert window["extremes_are_lower_bounds"]


def test_short_order_and_completed_windows_do_not_use_future_data():
    candles = [candle(0, 89, 105), candle(5, 95, 115), candle(15, 1, 999)]
    result = track_signal(signal("SHORT"), candles, [], START + timedelta(minutes=15))
    window = result["windows"]["15"]
    assert window["target_stop_order"]["tp1"] == "target_before_stop"
    assert window["mfe_r"] == "1.1"
    assert window["mae_r"] == "1.5"
    assert window["status"] == "complete"
    assert result["windows"]["30"]["status"] == "pending"


def test_horizon_boundary_samples_and_cost_hypothesis():
    start = START + timedelta(seconds=1)
    now = start + timedelta(minutes=15)
    result = track_signal(
        signal(start=start), [], [tick(2, 101), tick(900, 110), tick(902, 999)], now, D(5), D(2)
    )
    window = result["windows"]["15"]
    assert window["hypothetical_gross_r"] == "1"
    assert D(window["hypothetical_net_r"]) == D("0.9853")
    assert window["coverage"] == "incomplete"
    assert window["exit_observation"]["price"] == "110"


def test_replay_is_deterministic_and_full_ohlc_has_exact_extremes():
    candles = [candle(0, 95, 105), candle(5, 96, 115), candle(10, 97, 109)]
    now = START + timedelta(minutes=15)
    a = track_signal(signal(), candles, [], now)
    b = track_signal(signal(), candles, [], now)
    assert a == b
    assert a["windows"]["15"]["mfe_r"] == "1.5"
    assert not a["windows"]["15"]["extremes_are_lower_bounds"]


async def test_tracker_reloads_durable_signals_and_waits_for_final_bar():
    stored = signal(start=START + timedelta(seconds=1))
    stored.id, stored.symbol = "stored-id", "BTCUSDT"
    repository = SimpleNamespace(
        tracking_signals=AsyncMock(return_value=[stored]),
        tracking_prices=AsyncMock(return_value=([], [])),
        save_outcomes=AsyncMock(),
    )
    settings = SimpleNamespace(tracking_fee_bps_per_side=0, tracking_slippage_bps_per_side=0)
    tracker = OutcomeTracker(repository, settings)
    await tracker.update(START + timedelta(minutes=123))
    assert repository.save_outcomes.call_args.args[2] is False
    # A fresh tracker after restart reconstructs progress from the same persisted observations.
    restarted = OutcomeTracker(repository, settings)
    await restarted.update(START + timedelta(minutes=126))
    assert repository.save_outcomes.call_args.args[2] is True
    result = repository.save_outcomes.call_args.args[1]
    assert all(w["coverage"] == "incomplete" for w in result["windows"].values())
