import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from btc_monitor.domain import ServiceStatus
from btc_monitor.runtime import MonitorRuntime


def runtime():
    instance = MonitorRuntime.__new__(MonitorRuntime)
    instance.settings = SimpleNamespace(
        ticker_persist_seconds=5, symbol="BTCUSDT", cvd_warmup_bars=12, ws_alert_seconds=60
    )
    instance.ticker = {"last_price": 100}
    instance.last_error = None
    instance._stream_down_since = {}
    instance._stream_warned = set()
    instance.status = ServiceStatus.RUNNING
    return instance


async def test_ticker_persistence_recovers_after_transient_database_failure(monkeypatch):
    instance = runtime()
    instance.repository = SimpleNamespace(save_ticker=AsyncMock(side_effect=[RuntimeError(), None]))
    monkeypatch.setattr(
        "btc_monitor.runtime.asyncio.sleep",
        AsyncMock(side_effect=[None, None, asyncio.CancelledError()]),
    )
    with pytest.raises(asyncio.CancelledError):
        await instance._ticker_persist_loop()
    assert instance.repository.save_ticker.await_count == 2


async def test_health_loop_recovers_and_does_not_die_on_database_failure(monkeypatch):
    instance = runtime()
    instance.repository = SimpleNamespace(
        flow_counts=AsyncMock(side_effect=[RuntimeError(), {"spot": 60, "linear": 60}])
    )
    monkeypatch.setattr(
        "btc_monitor.runtime.asyncio.sleep",
        AsyncMock(side_effect=[None, None, asyncio.CancelledError()]),
    )
    with pytest.raises(asyncio.CancelledError):
        await instance._health_loop()
    assert instance.repository.flow_counts.await_count == 2
    assert instance.status == ServiceStatus.RUNNING


async def test_telegram_failure_does_not_break_stream_recovery():
    instance = runtime()
    instance.telegram = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("temporary")))
    instance._stream_warned.add("spot")
    await instance._stream_state("spot", True)
    assert "spot" not in instance._stream_warned
    assert instance.last_error == "telegram_notification: RuntimeError"
