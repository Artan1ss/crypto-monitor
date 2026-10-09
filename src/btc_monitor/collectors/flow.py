from __future__ import annotations

import asyncio
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

    async def initialize(self) -> None:
        self._cvd = {
            Market.SPOT: await self.repository.latest_cvd(Market.SPOT, self.symbol),
            Market.LINEAR: await self.repository.latest_cvd(Market.LINEAR, self.symbol),
        }

    async def add(self, trade: Trade) -> None:
        to_flush: FlowBar | None = None
        async with self._lock:
            bucket = minute_bucket(trade.timestamp)
            bar = self._bars.get(trade.market)
            if bar is None or bar.bucket_start != bucket:
                to_flush = bar
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

    async def flush(self) -> None:
        async with self._lock:
            bars = list(self._bars.values())
        for bar in bars:
            await self.repository.save_flow(bar)

    def cvd(self, market: Market) -> Decimal:
        return self._cvd[market]
