from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from btc_monitor.archive.types import Instrument, Observation, PublicTrade, gap, minute


@dataclass
class Bucket:
    buy_base: Decimal = Decimal(0)
    sell_base: Decimal = Decimal(0)
    buy_quote: Decimal = Decimal(0)
    sell_quote: Decimal = Decimal(0)
    count: int = 0  # Number of feed trade/aggregate-trade records, not necessarily fills.
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    first_time: datetime | None = None
    last_time: datetime | None = None
    received_at: datetime | None = None
    max_delivery_lag_ms: int = 0
    reasons: set[str] = field(default_factory=set)

    def add(self, trade: PublicTrade):
        if trade.taker_side == "buy":
            self.buy_base += trade.base_size
            self.buy_quote += trade.price * trade.base_size
        else:
            self.sell_base += trade.base_size
            self.sell_quote += trade.price * trade.base_size
        self.count += 1
        self.high = max(self.high or trade.price, trade.price)
        self.low = min(self.low or trade.price, trade.price)
        if self.first_time is None or trade.event_time < self.first_time:
            self.first_time, self.open = trade.event_time, trade.price
        if self.last_time is None or trade.event_time >= self.last_time:
            self.last_time, self.close = trade.event_time, trade.price
        self.received_at = max(self.received_at or trade.received_at, trade.received_at)
        self.max_delivery_lag_ms = max(
            self.max_delivery_lag_ms,
            int((trade.received_at - trade.event_time).total_seconds() * 1000),
        )


class MinuteCapture:
    """Bounded in-memory minute capture. Storage IO is handled by a different task."""

    def __init__(self, instrument: Instrument, session: str, start: datetime, grace_seconds=10):
        self.instrument, self.session = instrument, session
        self.cursor = minute(start)
        self.grace = timedelta(seconds=grace_seconds)
        self.connected_since: datetime | None = None
        self.buckets: dict[datetime, Bucket] = {}
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.sequence: int | None = None
        self.last_trade_at: datetime | None = None
        self.pending_gaps: list[Observation] = []

    def connection(self, connected: bool, now: datetime):
        self.invalidate(minute(now), "reconnect_boundary" if connected else "disconnected", now)
        self.connected_since = now if connected else None
        self.sequence = None

    def invalidate(self, start: datetime, reason: str, received: datetime | None = None):
        bucket = minute(start)
        if bucket < self.cursor:
            # A finalized row is immutable; an overlapping quality observation invalidates it.
            if not self.pending_gaps or self.pending_gaps[-1].payload["reason"] != reason:
                self.pending_gaps.append(
                    gap(
                        self.instrument,
                        bucket,
                        bucket + timedelta(minutes=1),
                        received or start,
                        self.session,
                        reason,
                    )
                )
            else:
                previous = self.pending_gaps[-1]
                self.pending_gaps[-1] = gap(
                    self.instrument,
                    min(previous.event_time, bucket),
                    max(previous.end_time, bucket + timedelta(minutes=1)),
                    received or start,
                    self.session,
                    reason,
                )
        else:
            self.buckets.setdefault(bucket, Bucket()).reasons.add(reason)

    def add(self, trade: PublicTrade):
        if trade.instrument.key != self.instrument.key:
            raise ValueError("Mixed instruments")
        if trade.trade_id in self.seen:
            return
        if len(self.buckets) >= 120 and minute(trade.event_time) not in self.buckets:
            raise BufferError("Minute capture full; stop feed rather than invent complete data")
        self.seen[trade.trade_id] = None
        if len(self.seen) > 100000:
            self.seen.popitem(last=False)
        bucket = minute(trade.event_time)
        if trade.event_time > trade.received_at + timedelta(seconds=5):
            self.invalidate(minute(trade.received_at), "future_timestamp", trade.received_at)
            return
        if bucket < self.cursor:
            self.invalidate(bucket, "late_trade_after_finalization", trade.received_at)
            return
        # Reject stale replay rather than letting it grow memory after a long DB outage.
        if trade.received_at - trade.event_time > timedelta(minutes=2):
            self.invalidate(bucket, "stale_trade", trade.received_at)
            return
        if trade.sequence is not None:
            if self.sequence is not None and trade.sequence != self.sequence + 1:
                self.invalidate(bucket, "trade_id_gap_or_reorder", trade.received_at)
                if self.last_trade_at:
                    self.invalidate(
                        minute(self.last_trade_at), "trade_id_gap_or_reorder", trade.received_at
                    )
            self.sequence = max(self.sequence or trade.sequence, trade.sequence)
        if self.connected_since is None:
            self.invalidate(bucket, "unacknowledged_subscription")
        self.last_trade_at = trade.event_time
        self.buckets.setdefault(bucket, Bucket()).add(trade)

    def finalized(self, now: datetime, max_minutes: int = 10) -> list[Observation]:
        result, self.pending_gaps = self.pending_gaps, []
        for _ in range(max_minutes):
            end = self.cursor + timedelta(minutes=1)
            if end + self.grace > now:
                break
            bucket = self.buckets.pop(self.cursor, Bucket())
            if self.connected_since is None or self.connected_since > self.cursor:
                bucket.reasons.add("not_continuously_connected")
            # Empty/quiet liquid BTC feeds are not taken as evidence of zero activity.
            if bucket.count == 0:
                bucket.reasons.add("no_trade_evidence")
            payload = {
                "schema_version": 1,
                "base_currency": "BTC",
                "quote_currency": self.instrument.quote,
                "buy_base": bucket.buy_base,
                "sell_base": bucket.sell_base,
                "delta_base": bucket.buy_base - bucket.sell_base,
                "buy_quote": bucket.buy_quote,
                "sell_quote": bucket.sell_quote,
                "delta_quote": bucket.buy_quote - bucket.sell_quote,
                "open": bucket.open,
                "high": bucket.high,
                "low": bucket.low,
                "close": bucket.close,
                "record_count": bucket.count,
                "first_trade_at": bucket.first_time,
                "last_trade_at": bucket.last_time,
                "last_trade_received_at": bucket.received_at,
                "max_delivery_lag_ms": bucket.max_delivery_lag_ms,
                "quality_reasons": sorted(bucket.reasons),
                "contract_size_btc": self.instrument.size_multiplier,
                "coverage_scope": "subscribed_public_trade_feed",
            }
            result.append(
                Observation(
                    self.instrument.venue,
                    self.instrument.market,
                    self.instrument.symbol,
                    "flow",
                    60,
                    self.cursor,
                    end,
                    now,
                    "live",
                    self.session,
                    "partial" if bucket.reasons else "complete",
                    payload,
                )
            )
            self.cursor = end
        return result
