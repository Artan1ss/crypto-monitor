from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from btc_monitor.db import Database
from btc_monitor.domain import Candle, FlowBar, Market, Signal
from btc_monitor.models import Candle1h, Candle5m, MarketFlow1m, SignalRecord, TickerSnapshot


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def save_flow(self, bar: FlowBar) -> None:
        values = {
            "market": bar.market.value,
            "symbol": bar.symbol,
            "bucket_start": bar.bucket_start,
            "buy_notional": bar.buy_notional,
            "sell_notional": bar.sell_notional,
            "delta_notional": bar.delta_notional,
            "cumulative_cvd": bar.cumulative_cvd,
            "trade_count": bar.trade_count,
            "created_at": datetime.now(UTC),
            "is_complete": bar.is_complete,
        }
        statement = insert(MarketFlow1m).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                MarketFlow1m.market,
                MarketFlow1m.symbol,
                MarketFlow1m.bucket_start,
            ],
            set_=values,
        )
        async with self.database.session() as session:
            await session.execute(statement)

    async def latest_cvd(self, market: Market, symbol: str) -> Decimal:
        statement = (
            select(MarketFlow1m.cumulative_cvd)
            .where(MarketFlow1m.market == market.value, MarketFlow1m.symbol == symbol)
            .order_by(MarketFlow1m.bucket_start.desc())
            .limit(1)
        )
        async with self.database.session() as session:
            return (await session.scalar(statement)) or Decimal(0)

    async def latest_flow(self, market: Market, symbol: str):
        async with self.database.session() as session:
            return await session.scalar(
                select(MarketFlow1m)
                .where(MarketFlow1m.market == market.value, MarketFlow1m.symbol == symbol)
                .order_by(MarketFlow1m.bucket_start.desc())
                .limit(1)
            )

    async def invalidate_flow_minute(self, market, symbol, bucket):
        async with self.database.session() as session:
            await session.execute(
                update(MarketFlow1m)
                .where(
                    MarketFlow1m.market == market.value,
                    MarketFlow1m.symbol == symbol,
                    MarketFlow1m.bucket_start == bucket,
                )
                .values(is_complete=False)
            )

    async def flow_count(self, symbol: str) -> int:
        statement = (
            select(func.count()).select_from(MarketFlow1m).where(MarketFlow1m.symbol == symbol)
        )
        async with self.database.session() as session:
            return int((await session.scalar(statement)) or 0)

    async def flow_counts(self, symbol: str, minutes: int = 60) -> dict[str, int]:
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        statement = (
            select(MarketFlow1m.market, func.count())
            .where(
                MarketFlow1m.symbol == symbol,
                MarketFlow1m.is_complete.is_(True),
                MarketFlow1m.bucket_start >= end - timedelta(minutes=minutes),
                MarketFlow1m.bucket_start < end,
            )
            .group_by(MarketFlow1m.market)
        )
        async with self.database.session() as session:
            rows = (await session.execute(statement)).all()
        return {market: int(count) for market, count in rows}

    async def load_flow_rows(
        self, symbol: str, market: Market, limit: int = 500, end: datetime | None = None
    ) -> list[MarketFlow1m]:
        statement = (
            select(MarketFlow1m)
            .where(MarketFlow1m.symbol == symbol, MarketFlow1m.market == market.value)
            .order_by(MarketFlow1m.bucket_start.desc())
            .limit(limit)
        )
        if end is not None:
            statement = statement.where(MarketFlow1m.bucket_start < end)
        async with self.database.session() as session:
            rows = list((await session.scalars(statement)).all())
        return list(reversed(rows))

    async def save_candle(self, symbol: str, candle: Candle, interval: str, **features) -> None:
        model = Candle5m if interval == "5" else Candle1h
        values = {
            "symbol": symbol,
            "timestamp": candle.timestamp,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "created_at": datetime.now(UTC),
        }
        if interval == "5":
            values.update(turnover=candle.turnover, atr14=features.get("atr14"))
        else:
            values.update(ema50=features.get("ema50"), ema200=features.get("ema200"))
        statement = insert(model).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[model.symbol, model.timestamp], set_=values
        )
        async with self.database.session() as session:
            await session.execute(statement)

    async def load_candles(
        self, symbol: str, interval: str, limit: int, end: datetime | None = None
    ) -> list[Candle]:
        model = Candle5m if interval == "5" else Candle1h
        statement = (
            select(model)
            .where(model.symbol == symbol)
            .order_by(model.timestamp.desc())
            .limit(limit)
        )
        if end is not None:
            statement = statement.where(
                model.timestamp <= end - timedelta(minutes=5 if interval == "5" else 60)
            )
        async with self.database.session() as session:
            rows = list((await session.scalars(statement)).all())
        return [
            Candle(r.timestamp, r.open, r.high, r.low, r.close, r.volume, getattr(r, "turnover", 0))
            for r in reversed(rows)
        ]

    async def save_ticker(self, symbol: str, state: dict) -> None:
        async with self.database.session() as session:
            session.add(TickerSnapshot(symbol=symbol, **state))

    async def ticker_at(
        self, symbol: str, ago: timedelta, as_of: datetime | None = None
    ) -> TickerSnapshot | None:
        target = (as_of or datetime.now(UTC)) - ago
        statement = (
            select(TickerSnapshot)
            .where(TickerSnapshot.symbol == symbol, TickerSnapshot.timestamp <= target)
            .order_by(TickerSnapshot.timestamp.desc())
            .limit(1)
        )
        async with self.database.session() as session:
            return await session.scalar(statement)

    async def save_signal(self, signal: Signal) -> bool:
        values = {
            "id": signal.signal_id,
            "created_at": signal.generated_at or datetime.now(UTC),
            "signal_time": signal.timestamp,
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "score": signal.score,
            "strength": signal.strength.value,
            "entry_reference": signal.entry_reference,
            "stop_reference": signal.stop_reference,
            "tp1": signal.tp1,
            "tp2": signal.tp2,
            "tp3": signal.tp3,
            "reference_price": signal.reference_swing_price,
            "reference_timestamp": signal.reference_swing_timestamp,
            "sweep_extreme": signal.sweep_extreme,
            "strategy_version": signal.strategy_version,
            "reasons": signal.reasons,
            "features": signal.feature_payload(),
            "telegram_sent": False,
            "debug_snapshot": signal.debug_snapshot,
            "tracking_complete": False,
        }
        statement = (
            insert(SignalRecord)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    SignalRecord.direction,
                    SignalRecord.reference_timestamp,
                    SignalRecord.reference_price,
                ]
            )
            .returning(SignalRecord.id)
        )
        async with self.database.session() as session:
            inserted = await session.scalar(statement)
        return inserted is not None

    async def mark_telegram_sent(self, signal_id, message_id: str) -> None:
        async with self.database.session() as session:
            row = await session.get(SignalRecord, signal_id)
            if row:
                row.telegram_sent = True
                row.telegram_message_id = message_id
                row.telegram_sent_at = datetime.now(UTC)

    async def get_signal(self, signal_id):
        async with self.database.session() as session:
            return await session.get(SignalRecord, signal_id)

    async def recent_signals(self, limit=20):
        async with self.database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(SignalRecord).order_by(SignalRecord.created_at.desc()).limit(limit)
                    )
                ).all()
            )

    async def tracking_signals(self):
        async with self.database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(SignalRecord).where(SignalRecord.tracking_complete.is_(False))
                    )
                ).all()
            )

    async def tracking_prices(self, symbol: str, start: datetime, end: datetime):
        async with self.database.session() as session:
            candles = list(
                (
                    await session.scalars(
                        select(Candle5m)
                        .where(
                            Candle5m.symbol == symbol,
                            Candle5m.timestamp >= start - timedelta(minutes=5),
                            Candle5m.timestamp < end,
                        )
                        .order_by(Candle5m.timestamp)
                    )
                ).all()
            )
            ticks = list(
                (
                    await session.scalars(
                        select(TickerSnapshot)
                        .where(
                            TickerSnapshot.symbol == symbol,
                            TickerSnapshot.timestamp >= start,
                            TickerSnapshot.timestamp <= end,
                        )
                        .order_by(TickerSnapshot.timestamp)
                    )
                ).all()
            )
        return candles, ticks

    async def save_outcomes(self, signal_id, payload, complete):
        async with self.database.session() as session:
            await session.execute(
                update(SignalRecord)
                .where(SignalRecord.id == signal_id)
                .values(
                    outcomes=payload,
                    tracking_complete=complete,
                )
            )
