from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btc_monitor.collectors.flow import FlowAggregator
from btc_monitor.domain import Market, Trade

D = Decimal
START = datetime(2026, 1, 1, 12, tzinfo=UTC)


class MemoryRepository:
    def __init__(self):
        self.rows = {}

    async def latest_cvd(self, market, symbol):
        return D(0)

    async def latest_flow(self, market, symbol):
        return None

    async def save_flow(self, bar):
        self.rows[bar.bucket_start] = deepcopy(bar)

    async def invalidate_flow_minute(self, market, symbol, bucket):
        if bucket in self.rows:
            self.rows[bucket].is_complete = False


def trade(minutes, side="Buy"):
    return Trade(Market.SPOT, START + timedelta(minutes=minutes), side, D(100), D(1))


async def test_first_partial_minute_closed_minutes_and_current_flush():
    repo = MemoryRepository()
    flow = FlowAggregator("BTCUSDT", repo)
    await flow.initialize()
    await flow.add(trade(0.5))
    await flow.add(trade(1))
    assert not repo.rows[START].is_complete
    await flow.add(trade(2))
    assert repo.rows[START + timedelta(minutes=1)].is_complete
    await flow.flush()
    assert not repo.rows[START + timedelta(minutes=2)].is_complete


async def test_reconnect_invalidates_partial_and_next_full_minute_recovers():
    repo = MemoryRepository()
    flow = FlowAggregator("BTCUSDT", repo)
    await flow.initialize()
    await flow.add(trade(0))
    await flow.add(trade(1))
    await flow.invalidate(Market.SPOT)
    await flow.add(trade(5.5))
    await flow.add(trade(6))
    assert not repo.rows[START + timedelta(minutes=1)].is_complete
    assert not repo.rows[START + timedelta(minutes=5)].is_complete
    await flow.add(trade(7))
    assert repo.rows[START + timedelta(minutes=6)].is_complete


async def test_trade_ids_deduplicate_and_markets_are_independent():
    repo = MemoryRepository()
    flow = FlowAggregator("BTCUSDT", repo)
    await flow.initialize()
    item = Trade(Market.SPOT, START, "Buy", D(100), D(1), "trade-123")
    await flow.add(item)
    await flow.add(item)
    await flow.add(Trade(Market.LINEAR, START, "Buy", D(100), D(1), "trade-123"))
    assert flow.cvd(Market.SPOT) == D(100)
    assert flow.cvd(Market.LINEAR) == D(100)
    assert flow.ingestion_stats[Market.SPOT]["duplicates_ignored"] == 1


async def test_late_message_does_not_roll_back_or_double_count_cvd():
    repo = MemoryRepository()
    flow = FlowAggregator("BTCUSDT", repo)
    await flow.initialize()
    await flow.add(trade(0))
    await flow.add(trade(1))
    await flow.add(trade(0.9))
    assert flow.cvd(Market.SPOT) == D(200)
    await flow.add(trade(2))
    assert not repo.rows[START + timedelta(minutes=1)].is_complete
