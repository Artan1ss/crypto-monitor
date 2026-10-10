import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from btc_monitor.config import PROJECT_ROOT, Settings
from btc_monitor.domain import Candle, Market, Pivot, ServiceStatus
from btc_monitor.runtime import MonitorRuntime


def test_warmup_requires_twelve_five_minute_bars_per_market():
    runtime = MonitorRuntime.__new__(MonitorRuntime)
    runtime.settings = Settings(cvd_warmup_bars=12)
    assert runtime._warmup_complete({"spot": 59, "linear": 60}) is False
    assert runtime._warmup_complete({"spot": 60, "linear": 60}) is True


def test_env_file_is_resolved_from_project_root():
    assert Path(Settings.model_config["env_file"]) == PROJECT_ROOT / ".env"


def test_structure_target_excludes_pivots_breached_after_confirmation():
    runtime = MonitorRuntime.__new__(MonitorRuntime)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    runtime.candles_5m = [
        Candle(
            start + timedelta(minutes=i * 5),
            Decimal(100),
            Decimal(high),
            Decimal(90),
            Decimal(100),
            Decimal(1),
            Decimal(1),
        )
        for i, high in enumerate((110, 105, 112, 108))
    ]
    stale = Pivot(start, Decimal(110), 0, "high")
    active = Pivot(start + timedelta(minutes=10), Decimal(112), 2, "high")
    pivots, debug = runtime._active_structure([stale, active])
    assert pivots == [active]
    assert debug[0]["first_breach_at"] == start + timedelta(minutes=10)


async def test_signal_snapshot_captures_inputs_and_parameters_without_secrets(monkeypatch):
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    repo = SimpleNamespace(
        load_flow_rows=AsyncMock(), ticker_at=AsyncMock(), save_signal=AsyncMock(return_value=True)
    )
    rows = [
        SimpleNamespace(
            bucket_start=start + timedelta(minutes=i),
            delta_notional=Decimal(10),
            cumulative_cvd=Decimal((i + 1) * 10),
            is_complete=True,
        )
        for i in range(106)
    ]
    repo.load_flow_rows.return_value = rows

    def ticker(symbol, ago, as_of):
        return SimpleNamespace(
            timestamp=as_of - ago,
            open_interest=Decimal(100),
            funding_rate=Decimal(0),
            last_price=Decimal(101),
        )

    repo.ticker_at.side_effect = ticker
    settings = Settings(
        telegram_enabled=False,
        telegram_bot_token="secret-token",
        telegram_chat_id="secret-chat",
        database_url="postgres://secret-db",
    )
    runtime = MonitorRuntime(settings, repo)
    runtime.status = ServiceStatus.RUNNING
    runtime.candles_5m = [
        Candle(
            start + timedelta(minutes=(i - 80) * 5),
            Decimal(100),
            Decimal(102),
            Decimal(99),
            Decimal(101),
            Decimal(1),
            Decimal(100),
        )
        for i in range(101)
    ]
    runtime.candles_5m[-1] = Candle(
        start + timedelta(minutes=100),
        Decimal(100),
        Decimal(102),
        Decimal(98),
        Decimal(101),
        Decimal(1),
        Decimal(200),
        atr14=Decimal(2),
    )
    pivot = Pivot(start + timedelta(minutes=70), Decimal(100), 94, "low")
    monkeypatch.setattr("btc_monitor.runtime.confirmed_pivots", lambda *args: ([pivot], []))
    try:
        await runtime._evaluate(Decimal(2))
        saved = repo.save_signal.call_args.args[0]
        snapshot = saved.debug_snapshot
        assert snapshot["snapshot_origin"] == "live_at_decision"
        assert (
            snapshot["cvd"]["spot"]["reference_minute"]
            == (start + timedelta(minutes=74)).isoformat()
        )
        assert saved.spot_delta_5m == Decimal(50)
        assert snapshot["score_breakdown"]["total"] == str(saved.score)
        assert snapshot["decision"]["signal_id"] == str(saved.signal_id)
        assert snapshot["parameters"]["signal_min_score"] == 4.5
        assert len(snapshot["raw_flow_rows"]["spot"]) == 106
        serialized = json.dumps(snapshot)
        assert "secret-token" not in serialized
        assert "secret-chat" not in serialized
        assert "secret-db" not in serialized
    finally:
        await runtime.rest.close()
        await runtime.telegram.close()


async def test_candle_handler_returns_before_watermarks_and_worker_evaluates_after_both():
    repo = SimpleNamespace(save_candle=AsyncMock())
    runtime = MonitorRuntime(Settings(telegram_enabled=False), repo)
    runtime._evaluate = AsyncMock()
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    runtime.candles_5m = [
        Candle(
            start + timedelta(minutes=i * 5),
            Decimal(100),
            Decimal(102),
            Decimal(98),
            Decimal(101),
            Decimal(1),
            Decimal(1),
        )
        for i in range(20)
    ]
    candle = Candle(
        start + timedelta(minutes=100),
        Decimal(100),
        Decimal(102),
        Decimal(98),
        Decimal(101),
        Decimal(1),
        Decimal(1),
    )
    worker = asyncio.create_task(runtime._evaluation_loop())
    try:
        await asyncio.wait_for(runtime._on_candle("5", candle), timeout=1)
        runtime._evaluate.assert_not_awaited()
        end = candle.timestamp + timedelta(minutes=5)
        runtime.flow.watermarks[Market.SPOT] = end
        await asyncio.sleep(0.15)
        runtime._evaluate.assert_not_awaited()
        runtime.flow.watermarks[Market.LINEAR] = end
        await asyncio.wait_for(runtime._candle_queue.join(), timeout=1)
        runtime._evaluate.assert_awaited_once()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await runtime.rest.close()
        await runtime.telegram.close()
