# V1 Strategy Contract

## Purpose and invariants

V1 monitors Bybit BTCUSDT spot and USDT perpetual markets on a five-minute decision
timeframe with one-hour context. It is deterministic, reproducible, backtestable, and
analysis-only. No LLM participates in signal generation and no component can place an order.

The engine evaluates only finalized five-minute candles. A new alert requires a confirmed
liquidity sweep, at least one valid CVD divergence, healthy data, completed CVD warmup, an
unused reference swing, the score threshold, and the cooldown.

## Data and CVD

Bybit public trades provide taker side, price, and quantity. Each trade is normalized to
USDT notional:

`notional = price × quantity`

Buy takers contribute positive delta and sell takers negative delta. Spot and Linear flow
are accumulated independently into one-minute bars containing buy notional, sell notional,
delta, cumulative CVD, and trade count. The final cumulative value is restored from
PostgreSQL at startup. Five-minute deltas are derived from captured one-minute data.

Historical OHLCV is backfilled for ATR, EMA, volume, and market structure. Historical CVD is
never inferred from candles. A fresh service therefore stays in warmup for 12 five-minute
bars by default.

## Non-repainting structure

A pivot requires three completed bars on both sides by default. The most recent confirmed
pivot aged 6–72 bars is eligible. Because the right-side bars must already exist, pivots do
not repaint. Once a pivot creates a signal it is consumed.

## Sweep rules

For a bullish sweep, the finalized candle must trade below a confirmed pivot low, penetrate
by at least `max(0.05 × ATR14, close × 0.0001)` but no more than `1.25 × ATR14`, close back
above the pivot, and close at least 55% of the way up its range.

A bearish sweep is symmetric: price trades above a confirmed pivot high, respects the same
minimum/maximum penetration, closes back below it, and closes no higher than 45% of its
range.

ATR14 is Wilder's five-minute ATR. Fixed-dollar sweep distances are not used.

## CVD divergence

A bullish sweep needs current CVD above CVD at the swept pivot; a bearish sweep needs it
below. Strength is normalized by median absolute five-minute delta over about 20 bars:

`strength = favorable CVD difference / median(abs(delta_5m))`

Strength 0.5 is valid and 1.0 is strong. Spot and futures are evaluated separately. At least
one must be valid. Spot earns 2.0 score points and futures 1.5.

## Context and score

- Valid liquidity sweep: 2.0
- Spot CVD divergence: 2.0
- Futures CVD divergence: 1.5
- Supportive OI context: up to 1.0
- Funding: +0.5, neutral, or -0.5
- Turnover at least 1.30× the prior 20-bar median: 0.5
- Aligned one-hour EMA50/EMA200 trend: 0.5

Long OI contexts:

- Falling price plus OI at or below -0.25%: long-liquidation flush, +1.0.
- OI at or above +0.25%, negative futures delta, and reclaimed low: trapped new shorts, +0.75.

Short contexts are symmetric: short-covering exhaustion or trapped new longs.

Funding is context, never timing. For longs, funding at/below -0.0001 adds 0.5 and
at/above +0.0003 removes 0.5. Shorts use the symmetric rules.

The one-hour bonus requires close above EMA200 and EMA50 above EMA200 for long, or the
opposite for short. Countertrend sweeps remain eligible without the bonus.

Scores below 4.5 produce no signal. Scores from 4.5 to below 6 are MEDIUM; 6 or more are
STRONG. The mandatory CVD condition cannot be bypassed by score.

## Levels and deduplication

Entry reference is the confirmation close. Invalidation uses the sweep extreme plus a
buffer of `max(0.20 × ATR14, entry × 0.0005)`. TP1, TP2, and TP3 are 1R, 2R, and 3R.
The nearest confirmed opposing pivot is shown separately as a structure target.

Signals are unique by direction, reference timestamp, and reference price. An in-memory
consumed set prevents repeats during a run, the database uniqueness constraint protects
across concurrency/restarts, and a 30-minute secondary cooldown limits Telegram noise.

## Order book

When enabled, a Level-50 snapshot/delta book calculates bid and ask notional within 0.10%,
0.25%, and 0.50% of mid-price and reports `(bids - asks) / (bids + asks)`. This is display
context only and contributes no V1 score because displayed liquidity can be cancelled.

## Safety

The alert explicitly says no order was placed. Telegram credentials are optional and loaded
only from environment variables. Market data uses public endpoints. Every signal is stored,
including its reasons and complete feature payload, before external delivery is attempted.
