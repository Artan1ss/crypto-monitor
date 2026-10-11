from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    return value.astimezone(UTC)


def minute(value: datetime) -> datetime:
    return utc(value).replace(second=0, microsecond=0)


def timestamp_ms(value) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, UTC)


def decimal(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite market value")
    return result


def serializable(value):
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    return value


@dataclass(frozen=True)
class Instrument:
    venue: str
    market: str
    symbol: str
    quote: str = "USDT"
    # Only linear BTC contracts are supported; obtain contract multiplier from REST.
    size_multiplier: Decimal = Decimal(1)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.market}:{self.symbol}"


@dataclass(frozen=True)
class PublicTrade:
    instrument: Instrument
    event_time: datetime
    received_at: datetime
    trade_id: str
    taker_side: str
    price: Decimal
    base_size: Decimal
    sequence: int | None = None

    def __post_init__(self):
        utc(self.event_time)
        utc(self.received_at)
        if self.taker_side not in {"buy", "sell"} or not self.trade_id:
            raise ValueError("Invalid trade identity or taker side")
        if not self.price.is_finite() or not self.base_size.is_finite():
            raise ValueError("Non-finite trade")
        if self.price <= 0 or self.base_size <= 0:
            raise ValueError("Trade price/size must be positive")


@dataclass(frozen=True)
class Observation:
    venue: str
    market: str
    instrument: str
    dataset: str
    interval_seconds: int
    event_time: datetime
    end_time: datetime
    received_at: datetime
    source_type: str
    session_id: str
    quality: str
    payload: dict

    def __post_init__(self):
        for value in (self.event_time, self.end_time, self.received_at):
            utc(value)
        if self.end_time < self.event_time or self.interval_seconds < 0:
            raise ValueError("Invalid observation interval")
        if self.quality not in {"complete", "partial", "gap", "unknown"}:
            raise ValueError("Invalid quality")

    def values(self) -> dict:
        result = asdict(self)
        result["payload"] = serializable(result["payload"])
        # Receipt/session do not create duplicates for identical historical backfills.
        identity = {k: v for k, v in result.items() if k not in {"received_at", "session_id"}}
        if self.source_type == "live":
            identity["session_id"] = self.session_id
        result["fingerprint"] = hashlib.sha256(
            json.dumps(serializable(identity), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return result


def gap(
    instrument: Instrument,
    start: datetime,
    end: datetime,
    received: datetime,
    session: str,
    reason: str,
) -> Observation:
    return Observation(
        instrument.venue,
        instrument.market,
        instrument.symbol,
        "quality",
        0,
        start,
        max(end, start + timedelta(microseconds=1)),
        received,
        "live",
        session,
        "gap",
        {"reason": reason, "affects": "flow"},
    )
