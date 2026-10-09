from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from btc_monitor.domain import Candle, Market, Trade

log = structlog.get_logger()
MessageHandler = Callable[[dict], Awaitable[None]]
StateHandler = Callable[[str, bool], Awaitable[None]]


def reconnect_delay(attempt: int, jitter: float | None = None) -> float:
    """Exponential reconnect delay capped at 30 seconds plus bounded jitter."""
    return min(30.0, 2**attempt) + (random.uniform(0, 0.5) if jitter is None else jitter)


class BybitWebSocket:
    def __init__(
        self,
        name: str,
        url: str,
        topics: list[str],
        handler: MessageHandler,
        heartbeat_seconds: float = 20,
        stale_seconds: float = 30,
        state_handler: StateHandler | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.topics = topics
        self.handler = handler
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_seconds = stale_seconds
        self.state_handler = state_handler
        self.last_message_at: datetime | None = None
        self.last_reconnect_at: datetime | None = None
        self.reconnect_count = 0
        self.connected = False
        self._stopping = False

    async def run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                async with websockets.connect(
                    self.url, ping_interval=None, close_timeout=5, open_timeout=15
                ) as connection:
                    attempt = 0
                    await self._set_connected(True)
                    await connection.send(json.dumps({"op": "subscribe", "args": self.topics}))
                    self.last_message_at = datetime.now(UTC)
                    ping_task = asyncio.create_task(self._ping(connection))
                    watchdog_task = asyncio.create_task(self._watchdog(connection))
                    try:
                        async for raw in connection:
                            self.last_message_at = datetime.now(UTC)
                            message = json.loads(raw)
                            if "topic" in message:
                                await self.handler(message)
                        raise ConnectionError("WebSocket ended")
                    finally:
                        for task in (ping_task, watchdog_task):
                            task.cancel()
                        await asyncio.gather(ping_task, watchdog_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._set_connected(False)
                self.reconnect_count += 1
                self.last_reconnect_at = datetime.now(UTC)
                delay = reconnect_delay(attempt)
                log.warning("websocket_reconnecting", stream=self.name, delay=delay, error=str(exc))
                await asyncio.sleep(delay)
                attempt = min(attempt + 1, 5)
            else:
                attempt = 0

    async def _ping(self, connection: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await connection.send(json.dumps({"op": "ping"}))

    async def _watchdog(self, connection: ClientConnection) -> None:
        while True:
            await asyncio.sleep(min(5, self.stale_seconds / 2))
            if self.last_message_at:
                age = (datetime.now(UTC) - self.last_message_at).total_seconds()
                if age > self.stale_seconds:
                    await connection.close(code=1011, reason="stale stream")
                    return

    async def _set_connected(self, value: bool) -> None:
        changed = value != self.connected
        self.connected = value
        if changed and self.state_handler:
            await self.state_handler(self.name, value)

    async def stop(self) -> None:
        self._stopping = True


def parse_trades(message: dict, market: Market) -> list[Trade]:
    trades: list[Trade] = []
    for item in message.get("data", []):
        timestamp = datetime.fromtimestamp(int(item["T"]) / 1000, tz=UTC)
        trades.append(Trade(market, timestamp, item["S"], Decimal(item["p"]), Decimal(item["v"])))
    return trades


def parse_kline(message: dict) -> tuple[str, Candle] | None:
    topic = message.get("topic", "")
    interval = topic.split(".")[1] if topic.startswith("kline.") else ""
    data = message.get("data", [])
    if not data:
        return None
    item = data[0]
    return interval, Candle(
        timestamp=datetime.fromtimestamp(int(item["start"]) / 1000, tz=UTC),
        open=Decimal(item["open"]),
        high=Decimal(item["high"]),
        low=Decimal(item["low"]),
        close=Decimal(item["close"]),
        volume=Decimal(item["volume"]),
        turnover=Decimal(item["turnover"]),
        confirmed=item.get("confirm") is True or item.get("confirm") == "true",
    )


class BybitRestClient:
    def __init__(self, base_url: str) -> None:
        self.client = httpx.AsyncClient(base_url=base_url, timeout=15)

    async def klines(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        response = await self.client.get(
            "/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit API error: {payload.get('retMsg')}")
        rows = payload["result"]["list"]
        candles = [
            Candle(
                datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
                Decimal(row[1]),
                Decimal(row[2]),
                Decimal(row[3]),
                Decimal(row[4]),
                Decimal(row[5]),
                Decimal(row[6]),
                True,
            )
            for row in reversed(rows)
        ]
        minutes = 5 if interval == "5" else 60
        now = datetime.now(UTC)
        return [
            candle for candle in candles if candle.timestamp + timedelta(minutes=minutes) <= now
        ]

    async def close(self) -> None:
        await self.client.aclose()
