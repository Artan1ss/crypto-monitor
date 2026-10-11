from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from btc_monitor.db import Base


class MarketObservation(Base):
    """Append-only research archive. Never participates in V1 signal evaluation."""

    __tablename__ = "market_observations"
    __table_args__ = (
        UniqueConstraint("fingerprint"),
        Index("ix_observation_series_time", "venue", "market", "dataset", "event_time"),
        Index("ix_observation_available", "available_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(24), nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument: Mapped[str] = mapped_column(String(48), nullable=False)
    dataset: Mapped[str] = mapped_column(String(24), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quality: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class MarketFlow1m(Base):
    __tablename__ = "market_flow_1m"
    __table_args__ = (UniqueConstraint("market", "symbol", "bucket_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    buy_notional: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    sell_notional: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    delta_notional: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    cumulative_cvd: Mapped[Decimal] = mapped_column(Numeric(34, 8), nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_complete: Mapped[bool | None] = mapped_column(Boolean)


class Candle5m(Base):
    __tablename__ = "candles_5m"
    __table_args__ = (UniqueConstraint("symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    turnover: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    atr14: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Candle1h(Base):
    __tablename__ = "candles_1h"
    __table_args__ = (UniqueConstraint("symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    ema50: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    ema200: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TickerSnapshot(Base):
    __tablename__ = "ticker_snapshots"
    __table_args__ = (Index("ix_ticker_symbol_timestamp", "symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    last_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    index_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    open_interest: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    open_interest_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    funding_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    next_funding_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignalRecord(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_signal_time", "signal_time"),
        Index("ix_signals_direction", "direction"),
        Index("ix_signals_reference_timestamp", "reference_timestamp"),
        Index("ix_signals_tracking_complete", "tracking_complete"),
        UniqueConstraint("direction", "reference_timestamp", "reference_price"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    strength: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_reference: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    stop_reference: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    tp1: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    tp2: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    tp3: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    reference_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sweep_extreme: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False)
    telegram_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64))
    telegram_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    debug_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    outcomes: Mapped[dict | None] = mapped_column(JSONB)
    tracking_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
