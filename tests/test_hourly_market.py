from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from btc_monitor.archive.coinalyze import CoinalyzeSource, ProviderMarket
from btc_monitor.config import Settings
from btc_monitor.hourly.api import routes
from btc_monitor.hourly.features import universe
from btc_monitor.hourly.market import HourlyMarketFeed, boundary, display_metrics, parse_prices


def prices(now, count=4):
    return {
        "result": {
            "symbol": "BTCUSDT",
            "category": "linear",
            "list": [
                [
                    str(int((boundary(now) - timedelta(hours=i)).timestamp() * 1000)),
                    "100",
                    "110",
                    "90",
                    "105",
                    "20",
                    "2000",
                ]
                for i in range(count)
            ],
        }
    }


def test_price_validation_closed_forming_no_future_or_stale_or_wrong_market():
    now = datetime.now(UTC)
    p = prices(now)
    b = parse_prices(p, now)
    assert b[-1]["closed"] is False and b[-2]["closed"] is True
    assert b[-1]["price"]["close"] == 105
    with pytest.raises(ValueError, match="Stale"):
        parse_prices(prices(now - timedelta(hours=1)), now)
    p["result"]["list"].append(p["result"]["list"][0])
    with pytest.raises(ValueError, match="Invalid"):
        parse_prices(p, now)
    p = prices(now)
    p["result"]["list"][0][4] = "nan"
    with pytest.raises(ValueError, match="Invalid"):
        parse_prices(p, now)
    p = prices(now)
    p["result"]["symbol"] = "ETHUSDT"
    with pytest.raises(ValueError, match="Unexpected"):
        parse_prices(p, now)


def observations():
    now = datetime.now(UTC)
    start = boundary(now) - timedelta(hours=2)
    source = CoinalyzeSource(Settings(_env_file=None), None)
    result = []
    for inst in universe("coinalyze"):
        market = ProviderMarket(inst, inst.key, {"has_buy_sell_data": True}, "base")
        raw = {"o": "100", "h": "110", "l": "90", "c": "105", "v": "10", "bv": "6"}
        result.append(
            source._observation(
                market, "ohlcv-history", raw, start, start + timedelta(hours=1), now
            )
        )
        if inst.market == "linear":
            for t in (start - timedelta(hours=1), start):
                result.append(
                    source._observation(
                        market, "open-interest-history", raw, t, t + timedelta(hours=1), now
                    )
                )
            result.append(
                source._observation(
                    market, "funding-rate-history", raw, start, start + timedelta(hours=1), now
                )
            )
    return start, result


def test_metrics_fixed_cohort_and_no_signal_eligible_or_database_ids():
    start, raw = observations()
    result = display_metrics(raw, datetime.now(UTC))[start.isoformat()]
    assert result["complete"] and result["aggregated"]["spot"]["delta_btc"] == "6"
    assert result["aggregated"]["linear"]["delta_btc"] == "6"
    assert result["oi_btc"] is not None
    assert result["display_only"] and not result["causal"] and not result["strategy_eligible"]
    assert result["input_ids"] == []
    missing = [r for r in raw if not (r.venue == "okx" and r.dataset == "provider_flow")]
    b = display_metrics(missing, datetime.now(UTC))[start.isoformat()]
    assert b["aggregated"]["spot"]["complete"]
    assert b["aggregated"]["linear"]["delta_btc"] is None
    unknown = [
        replace(r, payload={**r.payload, "volume_unit": "unknown"})
        if r.venue == "bybit" and r.market == "spot"
        else r
        for r in raw
    ]
    b = display_metrics(unknown, datetime.now(UTC))[start.isoformat()]
    assert b["aggregated"]["spot"]["delta_btc"] is None
    assert b["aggregated"]["linear"]["complete"]


async def test_prices_do_not_depend_on_database_or_provider_and_stale_receipt_not_renewed():
    feed = HourlyMarketFeed(
        Settings(_env_file=None), SimpleNamespace(append=AsyncMock(side_effect=OSError()))
    )
    now = datetime.now(UTC)
    feed.rest.get = AsyncMock(return_value=prices(now))
    try:
        await feed.update_prices()
        await feed.persist([object()])
        view = await feed.view(2)
        assert len(view["bars"]) == 2 and view["bars"][-1]["closed"] is False
        assert view["health"]["storage_error"] == "OSError"
        assert view["health"]["price_fresh"] and not view["health"]["provider_fresh"]
        assert view["health"]["telegram_enabled"] is False
        at = feed.price_received_at
        await feed.view(2)
        assert feed.price_received_at == at
        assert not feed.health(at + timedelta(seconds=91))["price_fresh"]
        await feed.update_prices()
        assert feed.rest.get.await_count == 1
    finally:
        await feed.stop()


async def test_live_api_is_local_wired_only_and_does_not_query_failing_signal_database():
    repo = SimpleNamespace(alerts=AsyncMock(side_effect=ConnectionRefusedError()))
    live = SimpleNamespace(
        view=AsyncMock(return_value={"schema": "btc-hourly-market-v1", "bars": []})
    )
    app = FastAPI()
    app.include_router(routes(SimpleNamespace(), repo, live_feed=live))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/hourly/market?hours=168")).status_code == 200
        assert (await c.get("/hourly/market?hours=169")).status_code == 422
        repo.alerts.assert_not_awaited()
        live.view.side_effect = RuntimeError("secret-key-must-not-appear")
        response = await c.get("/hourly/market")
        assert response.status_code == 502 and "secret-key" not in response.text
    app = FastAPI()
    app.include_router(routes(SimpleNamespace(), repo))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/hourly/market")).status_code == 404


async def test_provider_refresh_keeps_fixed_hourly_cohort_and_retries_raw_persistence():
    feed = HourlyMarketFeed(Settings(_env_file=None))
    start, raw = observations()
    markets = [
        ProviderMarket(i, i.key, {"has_buy_sell_data": True}, "base") for i in universe("coinalyze")
    ]
    dataset = {
        "ohlcv-history": "provider_flow",
        "open-interest-history": "provider_oi",
        "funding-rate-history": "provider_funding",
    }

    async def rows(market, endpoint, a, z, interval):
        assert interval == "1hour"
        return [
            r
            for r in raw
            if (r.venue, r.market, r.instrument)
            == (market.instrument.venue, market.instrument.market, market.instrument.symbol)
            and r.dataset == dataset[endpoint]
        ]

    source = CoinalyzeSource(Settings(_env_file=None), None)
    source.markets, source.discovery_at = markets, datetime.now(UTC)
    source.needs_calibration = lambda: False
    source.observations = AsyncMock(side_effect=rows)
    feed.source = source
    feed.store = SimpleNamespace(append=AsyncMock(side_effect=OSError("database unavailable")))
    try:
        await feed.update_provider()
        assert feed.provider_generation == 1
        assert feed.metrics[start.isoformat()]["complete"]
        assert feed.storage_error == "OSError"
        assert len(source.observations.call_args_list) == 12
        first_args = source.observations.call_args_list[0].args
        assert first_args[3] - first_args[2] == timedelta(hours=169)
        feed.store.append = AsyncMock(return_value=1)
        source.observations.reset_mock()
        await feed.update_provider()
        assert feed.provider_generation == 2 and feed.storage_error is None
        args = source.observations.call_args_list[0].args
        assert args[3] - args[2] == timedelta(hours=3)
        assert feed.store.append.await_count > 0
    finally:
        await feed.stop()
