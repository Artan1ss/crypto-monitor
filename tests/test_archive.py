import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from btc_monitor.archive.cli import export_archive, parse_time, read_export
from btc_monitor.archive.exchanges import PublicRest, instruments, parse_message, subscription
from btc_monitor.archive.flow import MinuteCapture
from btc_monitor.archive.history import HistoryLoader, binance_bar, validate_range
from btc_monitor.archive.replay import replay_windows, window_features
from btc_monitor.archive.service import ResearchCollector
from btc_monitor.archive.types import Observation, PublicTrade, gap, serializable
from btc_monitor.config import Settings

D = Decimal
START = datetime(2026, 1, 1, tzinfo=UTC)
SPOT = instruments(("bybit",))[0]
LINEAR = instruments(("bybit",))[1]


def settings(**kwargs):
    return Settings(_env_file=None, telegram_enabled=False, **kwargs)


def trade(inst=SPOT, seconds=1, received=None, side="buy", trade_id="t1", seq=None):
    event = START + timedelta(seconds=seconds)
    return PublicTrade(inst, event, received or event, trade_id, side, D(100), D(2), seq)


def stored(obs, row_id=1, available=None):
    result = obs.values()
    result.update(id=row_id, available_at=available or obs.received_at)
    return result


def flow_row(inst=SPOT, minute_index=0, delta=1, row_id=1, source="live", available=None):
    start = START + timedelta(minutes=minute_index)
    obs = Observation(
        inst.venue,
        inst.market,
        inst.symbol,
        "flow",
        60,
        start,
        start + timedelta(minutes=1),
        start + timedelta(minutes=1, seconds=10),
        source,
        "test",
        "complete",
        {
            "buy_base": D(3),
            "sell_base": D(3) - delta,
            "delta_base": D(delta),
            "open": D(100),
            "high": D(110),
            "low": D(90),
            "close": D(105),
        },
    )
    return stored(obs, row_id, available)


def test_universe_seven_streams_and_unknown_source_rejected():
    assert len(instruments(("bybit", "binance", "okx", "coinbase"))) == 7
    with pytest.raises(ValueError):
        instruments(("unknown",))
    config = settings(archive_venues=("bybit", "bybit"))
    with pytest.raises(ValueError):
        config.validate_runtime()
    assert settings().archive_enabled is False


@pytest.mark.parametrize("maker,expected", [(True, "sell"), (False, "buy")])
def test_binance_maker_flag_is_inverted(maker, expected):
    inst = instruments(("binance",))[0]
    message = {
        "e": "aggTrade",
        "s": "BTCUSDT",
        "T": int(START.timestamp() * 1000),
        "a": 1,
        "p": "100",
        "q": "2",
        "m": maker,
    }
    parsed = parse_message(inst, message, START)[0]
    assert parsed.taker_side == expected
    assert parsed.base_size == 2
    assert parsed.sequence == 1
    message["m"] = "false"
    with pytest.raises(ValueError):
        parse_message(inst, message, START)


def test_coinbase_inverts_maker_side_and_ignores_seed():
    inst = instruments(("coinbase",))[0]
    message = {
        "type": "match",
        "product_id": "BTC-USD",
        "side": "sell",
        "time": START.isoformat(),
        "trade_id": 4,
        "price": "100",
        "size": "2",
    }
    assert parse_message(inst, message, START)[0].taker_side == "buy"
    message["type"] = "last_match"
    assert parse_message(inst, message, START) == []


def test_okx_contracts_are_converted_to_btc_not_assumed_btc_sizes():
    inst = replace(instruments(("okx",))[1], size_multiplier=D("0.01"))
    message = {
        "arg": {"channel": "trades"},
        "data": [
            {
                "instId": inst.symbol,
                "tradeId": "4",
                "side": "sell",
                "px": "100",
                "sz": "10",
                "ts": int(START.timestamp() * 1000),
            }
        ],
    }
    parsed = parse_message(inst, message, START)[0]
    assert parsed.base_size == D("0.1")
    assert parsed.taker_side == "sell"


def test_bybit_taker_side_and_payload_symbol_validation():
    message = {
        "topic": "publicTrade.BTCUSDT",
        "data": [
            {
                "s": "BTCUSDT",
                "T": int(START.timestamp() * 1000),
                "i": "a",
                "S": "Buy",
                "p": "100",
                "v": "2",
            }
        ],
    }
    assert parse_message(SPOT, message, START)[0].taker_side == "buy"
    message["data"][0]["s"] = "ETHUSDT"
    with pytest.raises(ValueError):
        parse_message(SPOT, message, START)


@pytest.mark.parametrize("price,size", [("NaN", "1"), ("1", "-2"), ("0", "2")])
def test_invalid_trade_values_rejected(price, size):
    with pytest.raises(ValueError):
        PublicTrade(SPOT, START, START, "a", "buy", D(price), D(size))


def capture():
    result = MinuteCapture(SPOT, "session", START - timedelta(seconds=1))
    result.cursor = START
    result.connection(True, START - timedelta(seconds=1))
    result.pending_gaps.clear()  # Ignore the preceding minute's connection boundary.
    return result


def test_capture_dedup_grace_event_time_ohlc_and_empty_minute():
    cap = capture()
    cap.add(trade(seconds=20))
    cap.add(trade(seconds=20))
    cap.add(replace(trade(seconds=1, trade_id="t2", side="sell"), price=D(99)))
    assert cap.finalized(START + timedelta(seconds=69)) == []
    row = cap.finalized(START + timedelta(seconds=70))[0]
    assert row.quality == "complete"
    assert row.payload["record_count"] == 2
    assert row.payload["delta_base"] == 0
    assert row.payload["open"] == 99
    assert row.payload["close"] == 100
    row = cap.finalized(START + timedelta(seconds=130))[0]
    assert row.quality == "partial"
    assert "no_trade_evidence" in row.payload["quality_reasons"]


def test_startup_and_reconnection_minute_are_partial():
    cap = MinuteCapture(SPOT, "test", START)
    cap.connection(True, START + timedelta(seconds=5))
    cap.add(trade(seconds=10))
    assert cap.finalized(START + timedelta(seconds=70))[0].quality == "partial"


def test_future_and_late_timestamps_do_not_pollute_cvd():
    cap = capture()
    cap.add(trade(seconds=20, received=START))
    assert cap.finalized(START + timedelta(seconds=70))[0].payload["delta_base"] == 0
    cap.add(trade(seconds=21, received=START + timedelta(seconds=80), trade_id="late"))
    assert cap.pending_gaps[0].dataset == "quality"
    assert cap.pending_gaps[0].payload["reason"] == "late_trade_after_finalization"


def test_trade_sequence_jump_is_partial():
    cap = capture()
    cap.add(trade(seconds=10, seq=3))
    cap.add(trade(seconds=20, trade_id="t2", seq=5))
    assert cap.finalized(START + timedelta(seconds=70))[0].quality == "partial"


def test_historical_fingerprint_idempotent_but_live_sessions_separate():
    row = flow_row(source="rest_history")
    obs = Observation(**{k: row[k] for k in Observation.__dataclass_fields__})
    newer = replace(obs, received_at=START + timedelta(days=1), session_id="second")
    assert obs.values()["fingerprint"] == newer.values()["fingerprint"]
    live = replace(obs, source_type="live")
    assert (
        live.values()["fingerprint"] != replace(live, session_id="second").values()["fingerprint"]
    )


def test_replay_as_received_excludes_later_backfill_and_future_bar():
    end = START + timedelta(minutes=1)
    rows = [
        flow_row(source="rest_history", available=START + timedelta(days=1)),
        flow_row(LINEAR, delta=-1, row_id=2),
        flow_row(minute_index=1, row_id=3),
    ]
    strict = window_features(rows, [SPOT, LINEAR], START, end, end + timedelta(seconds=15))
    assert strict["aggregated"]["spot"]["delta_btc"] is None
    historical = window_features(rows, [SPOT, LINEAR], START, end, end, "historical")
    assert historical["spot_buy_futures_sell"]
    assert historical["causal"] is False


def test_duplicate_origin_never_double_counts_and_fixed_universe_detects_missing_venue():
    rows = [
        flow_row(),
        flow_row(row_id=2, source="rest_history"),
        flow_row(LINEAR, row_id=3, delta=-1),
    ]
    end = START + timedelta(minutes=1)
    report = window_features(rows, [SPOT, LINEAR], START, end, end + timedelta(seconds=15))
    assert report["aggregated"]["spot"]["delta_btc"] == 1
    missing = instruments(("binance",))[0]
    report = window_features(rows, [SPOT, LINEAR, missing], START, end, end + timedelta(seconds=15))
    assert report["aggregated"]["spot"]["delta_btc"] is None
    assert report["aggregated"]["spot"]["expected_venues"] == ["bybit", "binance"]


def test_late_quality_revision_does_not_rewrite_earlier_causal_replay():
    end = START + timedelta(minutes=1)
    rows = [flow_row(), flow_row(LINEAR, row_id=2, delta=-1)]
    quality = gap(SPOT, START, end, end + timedelta(seconds=30), "test", "late_trade")
    rows.append(stored(quality, 3))
    earlier = window_features(rows, [SPOT, LINEAR], START, end, end + timedelta(seconds=15))
    later = window_features(rows, [SPOT, LINEAR], START, end, end + timedelta(seconds=40))
    assert earlier["flow_complete"] is True
    assert later["flow_complete"] is False


@pytest.mark.parametrize("timeframe,minutes", [("5m", 5), ("1h", 60), ("4h", 240), ("1d", 1440)])
def test_timeframes_are_closed_utc_windows(timeframe, minutes):
    reports = list(
        replay_windows(
            [], [SPOT, LINEAR], START, START + timedelta(minutes=minutes), timeframe, "as_received"
        )
    )
    assert len(reports) == 1
    assert reports[0]["flows"][SPOT.key]["expected_minutes"] == minutes
    assert not reports[0]["flow_complete"]


def test_timezone_required_and_range_bounded():
    with pytest.raises(ValueError):
        parse_time("2026-01-01T00:00:00")
    assert parse_time("2025-12-31T19:00:00-05:00") == START
    with pytest.raises(ValueError):
        validate_range(START, START + timedelta(days=32))


def binance_raw(start=START):
    ts = int(start.timestamp() * 1000)
    return [ts, "100", "110", "90", "105", "10", ts + 59999, "1000", 5, "6", "600", "0"]


def test_binance_historical_cvd_uses_taker_buy_not_candle_direction():
    inst = instruments(("binance",))[0]
    row = binance_bar(inst, binance_raw(), START + timedelta(days=1), "test")
    assert row.payload["delta_base"] == 2
    assert row.payload["delta_quote"] == 200
    assert row.source_type == "rest_history"
    malformed = binance_raw()
    malformed[9] = "20"
    with pytest.raises(ValueError):
        binance_bar(inst, malformed, START, "test")


async def test_history_filters_open_and_out_of_range_candles(monkeypatch):
    inst = instruments(("binance",))[0]
    rest = SimpleNamespace(
        get=AsyncMock(return_value=[binance_raw(), binance_raw(START + timedelta(minutes=1))])
    )
    store = SimpleNamespace(append=AsyncMock(return_value=1))
    monkeypatch.setattr("btc_monitor.archive.history.asyncio.sleep", AsyncMock())
    result = await HistoryLoader(rest, store).prices_and_flow(
        inst, START, START + timedelta(minutes=1)
    )
    assert result["returned"] == 1
    assert len(store.append.call_args.args[0]) == 1


async def test_okx_metadata_dynamic_contract_multiplier():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [{"ctType": "linear", "ctValCcy": "BTC", "ctVal": "0.01", "ctMult": "1"}],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rest = PublicRest(settings(), client)
    try:
        inst, _ = await rest.metadata(instruments(("okx",))[1])
        assert inst.size_multiplier == D("0.01")
    finally:
        await rest.close()


async def test_bybit_both_sides_oi_normalized_separately_from_v1():
    ts = int(datetime.now(UTC).timestamp() * 1000)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "time": ts,
                "result": {
                    "list": [
                        {
                            "openInterest": "100",
                            "fundingRate": "-0.001",
                            "fundingIntervalHour": "4",
                            "nextFundingTime": ts + 14400000,
                        }
                    ]
                },
            },
        )

    rest = PublicRest(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        rows = await rest.derivatives(LINEAR, "test")
        assert rows[0].payload["base_oi_btc"] == 50
        assert rows[0].payload["raw_oi"] == 100
        assert rows[1].payload["rate_per_8h"] == D("-0.002")
    finally:
        await rest.close()


async def test_rest_429_retry_and_nonzero_exchange_code(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"retCode": 10001})

    monkeypatch.setattr("btc_monitor.archive.exchanges.asyncio.sleep", AsyncMock())
    rest = PublicRest(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(ValueError):
            await rest.get("bybit", "/v5/market/tickers")
        assert len(calls) == 2
    finally:
        await rest.close()


async def test_export_manifest_roundtrip_and_tamper_detection(tmp_path):
    row = flow_row()

    class Store:
        async def scan(self, start, end):
            yield row

    output = tmp_path / "archive.jsonl"
    manifest = await export_archive(Store(), START, START + timedelta(minutes=1), output, [SPOT])
    restored, loaded = read_export(output)
    assert restored[0]["event_time"] == START
    assert loaded["sha256"] == manifest["sha256"]
    with pytest.raises(FileExistsError):
        await export_archive(Store(), START, START + timedelta(minutes=1), output, [SPOT])
    output.write_text(json.dumps(serializable(flow_row(delta=2))) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_export(output)


async def test_archive_disabled_does_not_start_workers_or_send_notifications():
    collector = ResearchCollector(settings(), SimpleNamespace())
    await collector.start()
    assert collector.tasks == []
    assert collector.health()["enabled"] is False
    await collector.stop()


async def test_storage_failure_retries_same_batch_without_drop(monkeypatch):
    store = SimpleNamespace(append=AsyncMock(side_effect=[RuntimeError(), 1]))
    collector = ResearchCollector(settings(), store)
    row = Observation(**{k: flow_row()[k] for k in Observation.__dataclass_fields__})
    await collector.queue.put(row)
    monkeypatch.setattr("btc_monitor.archive.service.asyncio.sleep", AsyncMock())
    task = asyncio.create_task(collector._persist())
    for _ in range(10):
        await asyncio.sleep(0)
        # AsyncMock sleep does not yield; yield explicitly through a tiny Future callback.
        future = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(future.set_result, None)
        await future
        if collector.saved_rows:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert store.append.await_count == 2
    assert store.append.call_args_list[0].args[0] == store.append.call_args_list[1].args[0]
    assert collector.saved_rows == 1
    assert collector.last_storage_error is None
    await collector.rest.close()


def test_subscription_urls_are_public_and_coinbase_has_heartbeat():
    config = settings()
    for inst in instruments(config.archive_venues):
        url, message = subscription(inst, config)
        assert url.startswith("wss://")
        assert "token" not in url
        if inst.venue == "coinbase":
            assert "heartbeat" in message["channels"]


def test_replay_rejects_duplicate_universe_and_nonminute_boundaries():
    end = START + timedelta(minutes=1)
    with pytest.raises(ValueError):
        window_features([], [SPOT, SPOT], START, end, end)
    with pytest.raises(ValueError):
        window_features([], [SPOT], START + timedelta(seconds=1), end, end)


def test_gap_invalidates_live_price_but_explicit_history_can_repair():
    end = START + timedelta(minutes=1)
    decision = end + timedelta(seconds=15)
    live = flow_row(LINEAR)
    missing = stored(gap(LINEAR, START, end, decision, "test", "missing_trade"), 2)
    report = window_features([live, missing], [SPOT, LINEAR], START, end, decision)
    assert report["price"] is None
    history = flow_row(LINEAR, source="rest_history", row_id=3, available=decision)
    report = window_features([live, missing, history], [SPOT, LINEAR], START, end, decision)
    assert report["price"]["close"] == 105


def test_finalized_disconnect_gap_keeps_actual_detection_time():
    result = capture()
    result.cursor = START + timedelta(minutes=1)
    received = START + timedelta(seconds=59)
    result.connection(False, received)
    assert result.pending_gaps[0].received_at == received


async def test_queue_backpressure_cancellation_keeps_finalized_outbox():
    store = SimpleNamespace(append=AsyncMock(return_value=1), release_writer=AsyncMock())
    collector = ResearchCollector(settings(archive_venues=("bybit",)), store)
    collector.writer_lock = object()
    collector.queue = asyncio.Queue(1)
    rows = [
        Observation(**{k: flow_row(minute_index=n)[k] for k in Observation.__dataclass_fields__})
        for n in (0, 1)
    ]
    expected_times = {r.event_time for r in rows}
    for capture_state in collector.captures.values():
        capture_state.finalized = lambda now: []
    first = next(iter(collector.captures.values()))
    batches = iter([rows, []])
    first.finalized = lambda now: next(batches, [])
    task = asyncio.create_task(collector._finalize())
    collector.tasks.append(task)
    await asyncio.sleep(0)
    assert collector.queue.qsize() == 1
    assert len(collector._finalizing) == 1
    await collector.stop()
    persisted = store.append.call_args.args[0]
    assert {r.event_time for r in persisted} == expected_times
    assert len(persisted) == 2


async def test_archive_health_not_green_without_trade_and_storage_evidence():
    collector = ResearchCollector(settings(archive_enabled=True), SimpleNamespace())
    for state in collector.states.values():
        state["connected"] = True
    task = asyncio.create_task(asyncio.sleep(100))
    collector.tasks.append(task)
    assert collector.health()["healthy"] is False
    await collector.stop()
