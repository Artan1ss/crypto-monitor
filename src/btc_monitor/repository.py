from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
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

    async def flow_count(self, symbol: str) -> int:
        statement = (
            select(func.count()).select_from(MarketFlow1m).where(MarketFlow1m.symbol == symbol)
        )
        async with self.database.session() as session:
            return int((await session.scalar(statement)) or 0)

    async def flow_counts(self, symbol: str) -> dict[str, int]:
        statement = (
            select(MarketFlow1m.market, func.count())
            .where(MarketFlow1m.symbol == symbol)
            .group_by(MarketFlow1m.market)
        )
        async with self.database.session() as session:
            rows = (await session.execute(statement)).all()
        return {market: int(count) for market, count in rows}

    async def load_flow_rows(
        self, symbol: str, market: Market, limit: int = 500
    ) -> list[MarketFlow1m]:
        statement = (
            select(MarketFlow1m)
            .where(MarketFlow1m.symbol == symbol, MarketFlow1m.market == market.value)
            .order_by(MarketFlow1m.bucket_start.desc())
            .limit(limit)
        )
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

    async def load_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        model = Candle5m if interval == "5" else Candle1h
        statement = (
            select(model)
            .where(model.symbol == symbol)
            .order_by(model.timestamp.desc())
            .limit(limit)
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

    async def ticker_at(self, symbol: str, ago: timedelta) -> TickerSnapshot | None:
        target = datetime.now(UTC) - ago
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
            "created_at": datetime.now(UTC),
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
