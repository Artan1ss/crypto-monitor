import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from btc_monitor.archive.coinalyze import (
    CoinalyzeClient,
    CoinalyzeError,
    CoinalyzeSource,
    ProviderMarket,
    select_markets,
)
from btc_monitor.archive.exchanges import instruments
from btc_monitor.archive.replay import window_features
from btc_monitor.config import Settings

START = datetime(2026, 1, 1, tzinfo=UTC)
KEY = "test-only-do-not-log-this-key"
D = Decimal
SPOT, LINEAR = instruments(("binance",))


def config(**kwargs):
    return Settings(_env_file=None, coinalyze_api_key=KEY, **kwargs)


def market(inst=LINEAR, unit="base", **kwargs):
    metadata = {"symbol": "PROVIDER_BTC", "has_buy_sell_data": True, **kwargs}
    return ProviderMarket(inst, metadata["symbol"], metadata, unit)


def bar(start=START, **kwargs):
    return {
        "t": int(start.timestamp()),
        "o": "100",
        "h": "110",
        "l": "90",
        "c": "105",
        "v": "10",
        "bv": "6",
        "tx": 5,
        "btx": 3,
        **kwargs,
    }


def row(source, provider_market, endpoint="ohlcv-history", start=START, seconds=60, row_id=1):
    raw = bar(start)
    if endpoint == "liquidation-history":
        raw = {"t": int(start.timestamp()), "l": "0", "s": "2"}
    result = source._observation(
        provider_market,
        endpoint,
        raw,
        start,
        start + timedelta(seconds=seconds),
        START + timedelta(days=1),
    ).values()
    result.update(id=row_id, available_at=START + timedelta(days=1))
    return result


def test_key_excluded_from_config_dump_and_repr():
    settings = config()
    assert KEY not in repr(settings)
    assert KEY not in settings.model_dump_json()
    assert "coinalyze_api_key" not in settings.model_dump()
    assert Settings(_env_file=None, coinalyze_api_key=" ").coinalyze_api_key is None


def test_enable_requires_archive_and_key():
    with pytest.raises(ValueError, match="API_KEY"):
        Settings(_env_file=None, coinalyze_enabled=True).validate_runtime()
    with pytest.raises(ValueError, match="ARCHIVE_ENABLED"):
        config(coinalyze_enabled=True).validate_runtime()
    config(coinalyze_enabled=True, archive_enabled=True).validate_runtime()


async def test_header_only_fixed_origin_no_redirect_following():
    def handler(request):
        assert request.url.host == "api.coinalyze.net"
        assert KEY not in str(request.url)
        assert request.headers["api_key"] == KEY
        return httpx.Response(200, json=[])

    client = CoinalyzeClient(config(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        assert await client.get("exchanges") == []
        with pytest.raises(CoinalyzeError):
            await client.get("https://evil.example/")
        with pytest.raises(CoinalyzeError):
            await client.get("exchanges", {"api_key": KEY})
    finally:
        await client.close()


@pytest.mark.parametrize("status", [401, 403, 302])
async def test_error_body_and_auth_headers_never_exposed(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY, headers={"Location": "https://evil.example"})

    client = CoinalyzeClient(config(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(CoinalyzeError) as error:
            await client.get("exchanges")
        assert KEY not in str(error.value)
        assert error.value.status == status
        assert len(calls) == 1
    finally:
        await client.close()


async def test_rate_limit_counts_symbols_and_retry_after(monkeypatch):
    import btc_monitor.archive.coinalyze as module

    clock, sleeps, calls = [0.0], [], []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "75"})
            if len(calls) == 1
            else httpx.Response(200, json=[])
        )

    client = CoinalyzeClient(
        config(coinalyze_requests_per_minute=3),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await client.get("funding-rate", {"symbols": "A,B"})
        await client.get("exchanges")
        await client.get("exchanges")
        assert client.request_count == 4 and client.symbol_cost == 6
        assert sleeps[0] >= 75 and sleeps[1] >= 60
    finally:
        await client.close()


def test_metadata_mapping_filters_inverse_and_other_quotes_without_guessing_suffixes():
    exchanges = [{"name": "Binance", "code": "arbitrary-code"}]
    common = {
        "exchange": "arbitrary-code",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "symbol_on_exchange": "BTCUSDT",
        "has_buy_sell_data": True,
    }
    spot = [{**common, "symbol": "SPOT_ANY_SUFFIX", "unexpected_secret": KEY}]
    futures = [
        {
            **common,
            "symbol": "FUT_ANY_SUFFIX",
            "is_perpetual": True,
            "margined": "STABLE",
            "oi_lq_vol_denominated_in": "BASE_ASSET",
        },
        {**common, "symbol": "INVERSE", "is_perpetual": True, "margined": "COIN"},
        {**common, "symbol": "OTHER_QUOTE", "quote_asset": "USDC"},
    ]
    selected, missing = select_markets(
        exchanges, spot, futures, config(archive_venues=("binance",))
    )
    assert missing == []
    assert [m.provider_symbol for m in selected] == ["SPOT_ANY_SUFFIX", "FUT_ANY_SUFFIX"]
    assert [m.volume_unit for m in selected] == ["unknown", "base"]
    assert KEY not in json.dumps([m.metadata for m in selected])


@pytest.mark.parametrize(
    "unit,expected", [("base", D(2)), ("quote", None), ("contracts", None), ("unknown", None)]
)
def test_no_fake_btc_normalization(unit, expected):
    source = CoinalyzeSource(config(), SimpleNamespace())
    result = row(source, market(unit=unit))
    assert result["payload"]["delta_native"] == "2"
    assert result["payload"]["delta_btc"] == (str(expected) if expected is not None else None)
    assert result["dataset"] == "provider_flow"


def test_funding_percent_conversion_does_not_infer_settlement_interval():
    source = CoinalyzeSource(config(), SimpleNamespace())
    result = source._observation(
        market(),
        "funding-rate-history",
        bar(o="-0.001", h="-0.001", l="-0.001", c="-0.001"),
        START,
        START + timedelta(minutes=1),
        START + timedelta(days=1),
    )
    assert result.payload["rate_fraction"] == D("-0.00001")
    assert result.payload["rate_native"] == D("-0.001")
    assert result.payload["interval_hours"] is None
    assert result.payload["rate_kind"] == "reported_history_close"


@pytest.mark.parametrize("invalid", [{"bv": "11"}, {"v": "-1"}, {"c": "200"}, {"o": "NaN"}])
def test_invalid_values_rejected(invalid):
    source = CoinalyzeSource(config(), SimpleNamespace())
    with pytest.raises((CoinalyzeError, ValueError)):
        source._observation(
            market(),
            "ohlcv-history",
            bar(**invalid),
            START,
            START + timedelta(minutes=1),
            START + timedelta(days=1),
        )


async def test_history_seconds_inclusive_boundary_and_closed_bar_only():
    client = SimpleNamespace(
        get=AsyncMock(
            return_value=[
                {"symbol": "PROVIDER_BTC", "history": [bar(), bar(START + timedelta(minutes=1))]}
            ]
        )
    )
    source = CoinalyzeSource(config(), client)
    rows = await source.observations(market(), "ohlcv-history", START, START + timedelta(minutes=1))
    assert len(rows) == 1 and rows[0].event_time == START
    assert client.get.call_args.args[1]["to"] == int(START.timestamp()) + 59
    assert rows[0].source_type == "coinalyze_history"


async def test_unexpected_symbol_and_duplicate_timestamp_rejected():
    client = SimpleNamespace(get=AsyncMock(return_value=[{"symbol": "WRONG", "history": []}]))
    source = CoinalyzeSource(config(), client)
    with pytest.raises(CoinalyzeError, match="symbol"):
        await source.observations(market(), "ohlcv-history", START, START + timedelta(minutes=1))
    client.get.return_value = [{"symbol": "PROVIDER_BTC", "history": [bar(), bar()]}]
    with pytest.raises(CoinalyzeError, match="duplicate"):
        await source.observations(market(), "ohlcv-history", START, START + timedelta(minutes=1))


async def test_backfill_bounded_pagination_and_alignment():
    source = CoinalyzeSource(config(), SimpleNamespace())
    source.markets = [market(SPOT)]
    source.observations = AsyncMock(return_value=[])
    store = SimpleNamespace(append=AsyncMock(return_value=0))
    result = await source.backfill(store, START, START + timedelta(minutes=1001))
    assert source.observations.await_count == 2
    assert result["series"][0]["returned"] == 0
    assert result["series"][0]["expected_bars"] == 1001
    with pytest.raises(CoinalyzeError, match="align"):
        await source.backfill(store, START, START + timedelta(minutes=61), "1hour")


@pytest.mark.parametrize("mismatch", [False, True])
async def test_spot_calibration_requires_multiple_exact_time_volume_matches(monkeypatch, mismatch):
    from btc_monitor.archive.coinalyze import HistoryLoader

    source = CoinalyzeSource(config(), SimpleNamespace())
    source.markets = [market(SPOT, "unknown")]

    async def provider(market, endpoint, start, end):
        return [
            SimpleNamespace(
                event_time=start + timedelta(minutes=n),
                payload={"total_native": D(10 if not mismatch else 1000), "buy_native": D(6)},
            )
            for n in range(5)
        ]

    async def reference(self, inst, start, end):
        await self.store.append(
            [
                SimpleNamespace(
                    event_time=start + timedelta(minutes=n),
                    payload={"buy_base": D(6), "sell_base": D(4)},
                )
                for n in range(5)
            ]
        )

    source.observations = provider
    monkeypatch.setattr(HistoryLoader, "prices_and_flow", reference)
    await source.calibrate(SimpleNamespace())
    assert source.markets[0].volume_unit == ("unknown" if mismatch else "base")
    assert source.calibration[SPOT.key]["verified"] is not mismatch


def test_provider_evidence_is_causal_separate_and_not_double_counted():
    source = CoinalyzeSource(config(), SimpleNamespace())
    rows = [
        row(source, market(SPOT)),
        row(source, market(SPOT), row_id=2),
        row(source, market(LINEAR), row_id=3),
    ]
    end = START + timedelta(minutes=1)
    causal = window_features(rows, [SPOT, LINEAR], START, end, end)
    assert causal["providers"]["coinalyze"]["aggregated"]["spot"]["delta_btc"] is None
    historical = window_features(rows, [SPOT, LINEAR], START, end, end, "historical")
    assert historical["aggregated"]["spot"]["delta_btc"] is None
    assert historical["providers"]["coinalyze"]["aggregated"]["spot"]["delta_btc"] == 2


@pytest.mark.parametrize("seconds", [3600, 14400, 86400])
def test_coarse_history_is_usable_without_expanding_it_into_fake_minutes(seconds):
    source = CoinalyzeSource(config(), SimpleNamespace())
    rows = [row(source, market(SPOT), seconds=seconds)]
    end = START + timedelta(seconds=seconds)
    report = window_features(rows, [SPOT, LINEAR], START, end, end, "historical")
    flow = report["providers"]["coinalyze"]["flows"][SPOT.key]
    assert flow["complete"] and flow["expected_bars"] == 1 and flow["interval_seconds"] == seconds
    assert flow["price"]["close"] == 105
    report = window_features(
        rows, [SPOT, LINEAR], START, START + timedelta(minutes=5), end, "historical"
    )
    assert not report["providers"]["coinalyze"]["flows"][SPOT.key]["complete"]


def test_sparse_liquidations_are_not_claimed_complete_or_zero():
    source = CoinalyzeSource(config(), SimpleNamespace())
    rows = [row(source, market(), "liquidation-history")]
    end = START + timedelta(minutes=2)
    report = window_features(rows, [SPOT, LINEAR], START, end, end, "historical")
    outcome = report["providers"]["coinalyze"]["derivatives"][LINEAR.key]
    assert outcome["observed_short_liquidations_btc"] == 2
    assert outcome["short_liquidations_btc"] is None
    assert outcome["liquidation_coverage_complete"] is False


def test_unknown_liquidation_unit_never_becomes_zero_btc():
    source = CoinalyzeSource(config(), SimpleNamespace())
    rows = [row(source, market(unit="contracts"), "liquidation-history")]
    end = START + timedelta(minutes=1)
    report = window_features(rows, [SPOT, LINEAR], START, end, end, "historical")
    outcome = report["providers"]["coinalyze"]["derivatives"][LINEAR.key]
    assert outcome["observed_short_liquidations_btc"] is None
    assert outcome["short_liquidations_btc"] is None


def test_metadata_snapshot_retains_reference_evidence_without_credentials():
    source = CoinalyzeSource(config(), SimpleNamespace())
    source.markets = [market(SPOT)]
    source.calibration = {SPOT.key: {"verified": True, "matched_minutes": 5}}
    metadata = source.metadata_observation()
    assert metadata.payload["calibration"][SPOT.key]["verified"]
    assert metadata.dataset == "provider_metadata"
    assert KEY not in json.dumps(metadata.values()["payload"])


def test_oi_close_uses_end_timestamp_not_bar_start():
    source = CoinalyzeSource(config(), SimpleNamespace())
    rows = [row(source, market(), "open-interest-history", seconds=3600)]
    end = START + timedelta(minutes=5)
    report = window_features(
        rows, [SPOT, LINEAR], START, end, START + timedelta(hours=1), "historical"
    )
    assert not report["providers"]["coinalyze"]["derivatives"][LINEAR.key]["oi_fresh"]
