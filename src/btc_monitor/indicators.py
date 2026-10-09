from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from statistics import median

from btc_monitor.domain import ZERO, Candle, Direction, Divergence, Pivot, Sweep

EPSILON = Decimal("0.00000001")


def wilder_atr(candles: Sequence[Candle], period: int = 14) -> list[Decimal | None]:
    if not candles:
        return []
    true_ranges: list[Decimal] = []
    for i, candle in enumerate(candles):
        if i == 0:
            true_ranges.append(candle.high - candle.low)
        else:
            previous_close = candles[i - 1].close
            true_ranges.append(
                max(
                    candle.high - candle.low,
                    abs(candle.high - previous_close),
                    abs(candle.low - previous_close),
                )
            )
    values: list[Decimal | None] = [None] * len(candles)
    if len(candles) < period:
        return values
    atr = sum(true_ranges[:period], ZERO) / Decimal(period)
    values[period - 1] = atr
    for i in range(period, len(candles)):
        atr = ((atr * Decimal(period - 1)) + true_ranges[i]) / Decimal(period)
        values[i] = atr
    return values


def ema(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    output: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return output
    current = sum(values[:period], ZERO) / Decimal(period)
    output[period - 1] = current
    multiplier = Decimal(2) / Decimal(period + 1)
    for i in range(period, len(values)):
        current = (values[i] - current) * multiplier + current
        output[i] = current
    return output


def confirmed_pivots(
    candles: Sequence[Candle], left: int = 3, right: int = 3
) -> tuple[list[Pivot], list[Pivot]]:
    lows: list[Pivot] = []
    highs: list[Pivot] = []
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        candidate = candles[i]
        if candidate.low == min(c.low for c in window) and all(
            candidate.low < c.low for j, c in enumerate(window) if j != left
        ):
            lows.append(Pivot(candidate.timestamp, candidate.low, i, "low"))
        if candidate.high == max(c.high for c in window) and all(
            candidate.high > c.high for j, c in enumerate(window) if j != left
        ):
            highs.append(Pivot(candidate.timestamp, candidate.high, i, "high"))
    return lows, highs


def detect_sweep(candle: Candle, pivot: Pivot, atr: Decimal, direction: Direction) -> Sweep | None:
    if not candle.confirmed or atr <= ZERO:
        return None
    minimum = max(Decimal("0.05") * atr, candle.close * Decimal("0.0001"))
    maximum = Decimal("1.25") * atr
    candle_range = max(candle.high - candle.low, EPSILON)
    close_location = (candle.close - candle.low) / candle_range
    if direction == Direction.LONG:
        penetration = pivot.price - candle.low
        valid = (
            candle.low < pivot.price
            and minimum <= penetration <= maximum
            and candle.close > pivot.price
            and close_location >= Decimal("0.55")
        )
    else:
        penetration = candle.high - pivot.price
        valid = (
            candle.high > pivot.price
            and minimum <= penetration <= maximum
            and candle.close < pivot.price
            and close_location <= Decimal("0.45")
        )
    return Sweep(direction, pivot, candle, penetration) if valid else None


def cvd_divergence(
    current_cvd: Decimal,
    reference_cvd: Decimal | None,
    delta_5m_history: Sequence[Decimal],
    direction: Direction,
) -> Divergence:
    if reference_cvd is None or not delta_5m_history:
        return Divergence(False, ZERO)
    normalizer = Decimal(str(median([abs(v) for v in delta_5m_history[-20:]])))
    if direction == Direction.LONG:
        strength = (current_cvd - reference_cvd) / max(normalizer, EPSILON)
    else:
        strength = (reference_cvd - current_cvd) / max(normalizer, EPSILON)
    return Divergence(strength >= Decimal("0.5"), strength)


def percent_change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous in (None, ZERO):
        return None
    return (current - previous) / previous * Decimal(100)


def depth_imbalance(
    bids: dict[Decimal, Decimal], asks: dict[Decimal, Decimal], mid: Decimal, distance_pct: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    fraction = distance_pct / Decimal(100)
    min_bid = mid * (Decimal(1) - fraction)
    max_ask = mid * (Decimal(1) + fraction)
    bid_notional = sum((p * q for p, q in bids.items() if min_bid <= p <= mid), ZERO)
    ask_notional = sum((p * q for p, q in asks.items() if mid <= p <= max_ask), ZERO)
    total = bid_notional + ask_notional
    imbalance = (bid_notional - ask_notional) / total if total else ZERO
    return bid_notional, ask_notional, imbalance
