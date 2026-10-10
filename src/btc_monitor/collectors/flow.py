from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from btc_monitor.domain import FlowBar, Market, Trade
from btc_monitor.repository import Repository


def minute_bucket(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(second=0, microsecond=0)


class FlowAggregator:
    def __init__(self, symbol: str, repository: Repository) -> None:
        self.symbol = symbol
        self.repository = repository
        self._bars: dict[Market, FlowBar] = {}
        self._cvd: dict[Market, Decimal] = {Market.SPOT: Decimal(0), Market.LINEAR: Decimal(0)}
        self._lock = asyncio.Lock()
        self._valid: dict[Market, bool] = {}
        self.watermarks: dict[Market, datetime] = {}
        self._needs_partial = set(Market)
        self._seen_ids: set[tuple[Market, str]] = set()
        self._id_order: deque = deque()
        self.ingestion_stats = {
            m: {"duplicates_ignored": 0, "late_trades_ignored": 0} for m in Market
        }

    async def initialize(self) -> None:
        self._cvd = {
            Market.SPOT: await self.repository.latest_cvd(Market.SPOT, self.symbol),
            Market.LINEAR: await self.repository.latest_cvd(Market.LINEAR, self.symbol),
        }
        # Preserve persisted totals if a restart happens inside the same minute.
        for market in Market:
            row = await self.repository.latest_flow(market, self.symbol)
            if row and row.bucket_start == minute_bucket(datetime.now(UTC)):
                self._bars[market] = FlowBar(
                    market,
                    self.symbol,
                    row.bucket_start,
                    row.buy_notional,
                    row.sell_notional,
                    row.delta_notional,
                    row.cumulative_cvd,
                    row.trade_count,
                    False,
                )
                self._valid[market] = False

    async def invalidate(self, market: Market) -> None:
        async with self._lock:
            self._valid[market] = False
            if market in self._bars:
                self._bars[market].is_complete = False
            self.watermarks.pop(market, None)
            self._needs_partial.add(market)

    async def add(self, trade: Trade) -> None:
        to_flush: FlowBar | None = None
        async with self._lock:
            if trade.trade_id is not None:
                key = (trade.market, trade.trade_id)
                if key in self._seen_ids:
                    self.ingestion_stats[trade.market]["duplicates_ignored"] += 1
                    return
                self._seen_ids.add(key)
                self._id_order.append(key)
                if len(self._id_order) > 100_000:
                    self._seen_ids.remove(self._id_order.popleft())
            bucket = minute_bucket(trade.timestamp)
            bar = self._bars.get(trade.market)
            resumed = trade.market in self._needs_partial
            self._needs_partial.discard(trade.market)
            if bar is not None and bucket < bar.bucket_start:
                # Never roll CVD backwards into an earlier bucket on a late message.
                self._valid[trade.market] = False
                bar.is_complete = False
                self.ingestion_stats[trade.market]["late_trades_ignored"] += 1
                await self.repository.invalidate_flow_minute(trade.market, self.symbol, bucket)
                return
            if bar is None or bar.bucket_start != bucket:
                to_flush = (
                    replace(bar, is_complete=self._valid.get(trade.market, False)) if bar else None
                )
                self._valid[trade.market] = (
                    bar is not None
                    and not resumed
                    and (bucket - bar.bucket_start).total_seconds() == 60
                )
                bar = FlowBar(
                    trade.market, self.symbol, bucket, cumulative_cvd=self._cvd[trade.market]
                )
                self._bars[trade.market] = bar
            if trade.delta >= 0:
                bar.buy_notional += trade.notional
            else:
                bar.sell_notional += trade.notional
            bar.delta_notional += trade.delta
            self._cvd[trade.market] += trade.delta
            bar.cumulative_cvd = self._cvd[trade.market]
            bar.trade_count += 1
        if to_flush:
            await self.repository.save_flow(to_flush)
        self.watermarks[trade.market] = trade.timestamp

    async def flush(self) -> None:
        async with self._lock:
            bars = [replace(bar) for bar in self._bars.values()]
        for bar in bars:
            await self.repository.save_flow(bar)

    def cvd(self, market: Market) -> Decimal:
        return self._cvd[market]
