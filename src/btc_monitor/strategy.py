from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from btc_monitor.config import Settings
from btc_monitor.domain import (
    ZERO,
    Direction,
    Divergence,
    Pivot,
    Signal,
    SignalStrength,
    Sweep,
)


@dataclass(slots=True)
class StrategyContext:
    symbol: str
    sweep: Sweep
    atr: Decimal
    spot_divergence: Divergence
    futures_divergence: Divergence
    spot_delta_5m: Decimal
    futures_delta_5m: Decimal
    price_change_5m: Decimal
    oi_change_5m: Decimal | None
    oi_change_15m: Decimal | None
    funding_rate: Decimal | None
    previous_turnovers: list[Decimal]
    htf_close: Decimal | None
    htf_ema50: Decimal | None
    htf_ema200: Decimal | None
    opposing_pivots: list[Pivot]
    orderbook_imbalance: Decimal | None = None


class SignalEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, context: StrategyContext) -> Signal | None:
        if not (context.spot_divergence.valid or context.futures_divergence.valid):
            return None
        direction = context.sweep.direction
        score = Decimal("2.0")
        reasons = [self._sweep_reason(direction)]
        warnings: list[str] = []

        if context.spot_divergence.valid:
            score += Decimal("2.0")
            reasons.append(
                f"Spot CVD divergence ({context.spot_divergence.strength:+.2f} normalized)"
            )
        if context.futures_divergence.valid:
            score += Decimal("1.5")
            reasons.append(
                f"Futures CVD divergence ({context.futures_divergence.strength:+.2f} normalized)"
            )

        oi_score, oi_label = self._oi_context(context)
        score += oi_score
        if oi_label:
            reasons.append(oi_label.replace("_", " ").title())

        funding_score = self._funding_score(direction, context.funding_rate)
        score += funding_score
        if funding_score > 0:
            reasons.append("Funding is supportive")
        elif funding_score < 0:
            warnings.append("Funding is crowded against the setup")

        volume_ratio: Decimal | None = None
        if context.previous_turnovers:
            baseline = Decimal(str(median(context.previous_turnovers[-20:])))
            if baseline > ZERO:
                volume_ratio = context.sweep.candle.turnover / baseline
                if volume_ratio >= Decimal(str(self.settings.volume_multiplier)):
                    score += Decimal("0.5")
                    reasons.append(f"Volume spike ({volume_ratio:.2f}x median)")

        htf_trend, trend_aligned = self._htf_context(context)
        if trend_aligned:
            score += Decimal("0.5")
            reasons.append("1H trend aligned")
        elif htf_trend in {"BULLISH", "BEARISH"}:
            warnings.append(f"Countertrend setup: 1H is {htf_trend}")
        if not context.spot_divergence.valid:
            warnings.append("Spot CVD divergence is not confirmed")
        if not context.futures_divergence.valid:
            warnings.append("Futures CVD divergence is not confirmed")

        if score < Decimal(str(self.settings.signal_min_score)):
            return None

        candle = context.sweep.candle
        entry = candle.close
        extreme = candle.low if direction == Direction.LONG else candle.high
        buffer = max(Decimal("0.20") * context.atr, entry * Decimal("0.0005"))
        stop = extreme - buffer if direction == Direction.LONG else extreme + buffer
        risk = abs(entry - stop)
        sign = Decimal(1) if direction == Direction.LONG else Decimal(-1)
        structure_target = self._structure_target(entry, direction, context.opposing_pivots)
        strength = (
            SignalStrength.STRONG
            if score >= Decimal(str(self.settings.strong_signal_score))
            else SignalStrength.MEDIUM
        )
        return Signal(
            timestamp=candle.timestamp,
            symbol=context.symbol,
            direction=direction,
            strength=strength,
            score=score,
            entry_reference=entry,
            stop_reference=stop,
            tp1=entry + sign * risk,
            tp2=entry + sign * risk * Decimal(2),
            tp3=entry + sign * risk * Decimal(3),
            reference_swing_price=context.sweep.pivot.price,
            reference_swing_timestamp=context.sweep.pivot.timestamp,
            sweep_extreme=extreme,
            atr=context.atr,
            spot_cvd_divergence=context.spot_divergence.valid,
            spot_cvd_strength=context.spot_divergence.strength,
            futures_cvd_divergence=context.futures_divergence.valid,
            futures_cvd_strength=context.futures_divergence.strength,
            spot_delta_5m=context.spot_delta_5m,
            futures_delta_5m=context.futures_delta_5m,
            oi_change_5m=context.oi_change_5m,
            oi_change_15m=context.oi_change_15m,
            oi_context=oi_label,
            funding_rate=context.funding_rate,
            volume_ratio=volume_ratio,
            htf_trend=htf_trend,
            structure_target=structure_target,
            orderbook_imbalance=context.orderbook_imbalance,
            reasons=reasons,
            warnings=warnings,
            debug_snapshot={
                "score_breakdown": {
                    "sweep": "2.0",
                    "spot_divergence": "2.0" if context.spot_divergence.valid else "0",
                    "futures_divergence": "1.5" if context.futures_divergence.valid else "0",
                    "oi": str(oi_score),
                    "funding": str(funding_score),
                    "volume": "0.5"
                    if volume_ratio is not None
                    and volume_ratio >= Decimal(str(self.settings.volume_multiplier))
                    else "0",
                    "trend": "0.5" if trend_aligned else "0",
                    "total": str(score),
                },
                "risk": {
                    "buffer": str(buffer),
                    "risk_usdt": str(risk),
                    "structure_distance_r": str(abs(structure_target - entry) / risk)
                    if structure_target is not None and risk
                    else None,
                    "structure_is_display_only": True,
                },
            },
        )

    @staticmethod
    def _sweep_reason(direction: Direction) -> str:
        side = "Sell-side" if direction == Direction.LONG else "Buy-side"
        return f"{side} liquidity sweep and reclaim"

    def _oi_context(self, context: StrategyContext) -> tuple[Decimal, str | None]:
        threshold = Decimal(str(self.settings.oi_change_threshold_pct))
        oi = context.oi_change_5m
        if oi is None:
            return ZERO, None
        if context.sweep.direction == Direction.LONG:
            if context.price_change_5m < 0 and oi <= -threshold:
                return Decimal("1.0"), "LONG_LIQUIDATION_FLUSH"
            if oi >= threshold and context.futures_delta_5m < 0:
                return Decimal("0.75"), "NEW_SHORTS_TRAPPED"
        else:
            if context.price_change_5m > 0 and oi <= -threshold:
                return Decimal("1.0"), "SHORT_COVERING_EXHAUSTION"
            if oi >= threshold and context.futures_delta_5m > 0:
                return Decimal("0.75"), "NEW_LONGS_TRAPPED"
        return ZERO, None

    def _funding_score(self, direction: Direction, funding: Decimal | None) -> Decimal:
        if funding is None:
            return ZERO
        supportive = Decimal(str(self.settings.funding_supportive))
        extreme = Decimal(str(self.settings.funding_extreme))
        if direction == Direction.LONG:
            return (
                Decimal("0.5")
                if funding <= -supportive
                else Decimal("-0.5")
                if funding >= extreme
                else ZERO
            )
        return (
            Decimal("0.5")
            if funding >= supportive
            else Decimal("-0.5")
            if funding <= -extreme
            else ZERO
        )

    @staticmethod
    def _htf_context(context: StrategyContext) -> tuple[str, bool]:
        if None in (context.htf_close, context.htf_ema50, context.htf_ema200):
            return "UNKNOWN", False
        bullish = context.htf_close > context.htf_ema200 and context.htf_ema50 > context.htf_ema200
        bearish = context.htf_close < context.htf_ema200 and context.htf_ema50 < context.htf_ema200
        label = "BULLISH" if bullish else "BEARISH" if bearish else "MIXED"
        aligned = (context.sweep.direction == Direction.LONG and bullish) or (
            context.sweep.direction == Direction.SHORT and bearish
        )
        return label, aligned

    @staticmethod
    def _structure_target(
        entry: Decimal, direction: Direction, pivots: list[Pivot]
    ) -> Decimal | None:
        prices = [p.price for p in pivots]
        candidates = (
            [p for p in prices if p > entry]
            if direction == Direction.LONG
            else [p for p in prices if p < entry]
        )
        if not candidates:
            return None
        return min(candidates) if direction == Direction.LONG else max(candidates)
