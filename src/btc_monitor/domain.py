from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

ZERO = Decimal("0")


class Market(StrEnum):
    SPOT = "spot"
    LINEAR = "linear"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStrength(StrEnum):
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


class ServiceStatus(StrEnum):
    STARTING = "STARTING"
    WARMING_UP = "WARMING_UP"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"


@dataclass(slots=True, frozen=True)
class Trade:
    market: Market
    timestamp: datetime
    side: str
    price: Decimal
    quantity: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity

    @property
    def delta(self) -> Decimal:
        return self.notional if self.side.lower() == "buy" else -self.notional


@dataclass(slots=True)
class FlowBar:
    market: Market
    symbol: str
    bucket_start: datetime
    buy_notional: Decimal = ZERO
    sell_notional: Decimal = ZERO
    delta_notional: Decimal = ZERO
    cumulative_cvd: Decimal = ZERO
    trade_count: int = 0


@dataclass(slots=True, frozen=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal
    confirmed: bool = True
    atr14: Decimal | None = None


@dataclass(slots=True, frozen=True)
class Pivot:
    timestamp: datetime
    price: Decimal
    index: int
    kind: str
    cvd_spot: Decimal | None = None
    cvd_linear: Decimal | None = None


@dataclass(slots=True, frozen=True)
class Sweep:
    direction: Direction
    pivot: Pivot
    candle: Candle
    penetration: Decimal


@dataclass(slots=True, frozen=True)
class Divergence:
    valid: bool
    strength: Decimal


@dataclass(slots=True)
class Signal:
    timestamp: datetime
    symbol: str
    direction: Direction
    strength: SignalStrength
    score: Decimal
    entry_reference: Decimal
    stop_reference: Decimal
    tp1: Decimal
    tp2: Decimal
    tp3: Decimal
    reference_swing_price: Decimal
    reference_swing_timestamp: datetime
    sweep_extreme: Decimal
    atr: Decimal
    spot_cvd_divergence: bool
    spot_cvd_strength: Decimal
    futures_cvd_divergence: bool
    futures_cvd_strength: Decimal
    spot_delta_5m: Decimal
    futures_delta_5m: Decimal
    oi_change_5m: Decimal | None
    oi_change_15m: Decimal | None
    oi_context: str | None
    funding_rate: Decimal | None
    volume_ratio: Decimal | None
    htf_trend: str
    structure_target: Decimal | None
    orderbook_imbalance: Decimal | None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    strategy_version: str = "v1"
    signal_id: UUID = field(default_factory=uuid4)

    def feature_payload(self) -> dict[str, Any]:
        return {
            "atr": str(self.atr),
            "spot_cvd_divergence": self.spot_cvd_divergence,
            "spot_cvd_strength": str(self.spot_cvd_strength),
            "futures_cvd_divergence": self.futures_cvd_divergence,
            "futures_cvd_strength": str(self.futures_cvd_strength),
            "spot_delta_5m": str(self.spot_delta_5m),
            "futures_delta_5m": str(self.futures_delta_5m),
            "oi_change_5m": str(self.oi_change_5m) if self.oi_change_5m is not None else None,
            "oi_change_15m": str(self.oi_change_15m) if self.oi_change_15m is not None else None,
            "oi_context": self.oi_context,
            "funding_rate": str(self.funding_rate) if self.funding_rate is not None else None,
            "volume_ratio": str(self.volume_ratio) if self.volume_ratio is not None else None,
            "htf_trend": self.htf_trend,
            "structure_target": str(self.structure_target)
            if self.structure_target is not None
            else None,
            "orderbook_imbalance": str(self.orderbook_imbalance)
            if self.orderbook_imbalance is not None
            else None,
            "warnings": self.warnings,
        }
