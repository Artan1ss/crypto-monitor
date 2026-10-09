from decimal import Decimal

from btc_monitor.collectors.bybit import parse_kline, parse_trades, reconnect_delay
from btc_monitor.collectors.orderbook import LocalOrderBook
from btc_monitor.domain import Market


def test_trade_parser_uses_taker_side_and_usdt_notional():
    message = {"data": [{"T": 1_700_000_000_000, "S": "Sell", "p": "50000", "v": "0.2"}]}
    trade = parse_trades(message, Market.LINEAR)[0]
    assert trade.delta == Decimal("-10000.0")


def test_only_confirmed_kline_is_marked_confirmed():
    message = {
        "topic": "kline.5.BTCUSDT",
        "data": [
            {
                "start": 1_700_000_000_000,
                "open": "10",
                "high": "12",
                "low": "9",
                "close": "11",
                "volume": "2",
                "turnover": "21",
                "confirm": True,
            }
        ],
    }
    interval, candle = parse_kline(message)
    assert interval == "5"
    assert candle.confirmed is True


def test_orderbook_snapshot_delta_and_imbalance():
    book = LocalOrderBook()
    book.apply("snapshot", {"u": 1, "b": [["100", "2"]], "a": [["101", "1"]]})
    book.apply("delta", {"u": 2, "b": [["100", "0"], ["100.2", "3"]], "a": []})
    metrics = book.metrics()
    assert Decimal("100") not in book.bids
    assert metrics is not None
    assert metrics["imbalance_050"] > 0


def test_reconnect_backoff_is_exponential_and_capped():
    assert [reconnect_delay(i, jitter=0) for i in range(7)] == [1, 2, 4, 8, 16, 30, 30]
