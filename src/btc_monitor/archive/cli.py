"""Public probes, collection, bounded history jobs and reproducible research replay.

python -m btc_monitor.archive.cli --help
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import websockets

from btc_monitor.archive.coinalyze import (
    INTERVALS,
    CoinalyzeClient,
    CoinalyzeError,
    CoinalyzeSource,
)
from btc_monitor.archive.exchanges import PublicRest, instruments, parse_message, subscription
from btc_monitor.archive.history import HistoryLoader, validate_range
from btc_monitor.archive.replay import TIMEFRAMES, replay_windows
from btc_monitor.archive.service import ResearchCollector
from btc_monitor.archive.store import ArchiveStore
from btc_monitor.archive.types import serializable, utc
from btc_monitor.config import get_settings
from btc_monitor.db import Database
from btc_monitor.logging import configure_logging


def parse_time(value: str) -> datetime:
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


async def probe(settings, seconds=15) -> list[dict]:
    """No DB, keys, Telegram, or external writes. Verify parsers against public data."""
    rest = PublicRest(settings)

    async def one(inst):
        result = {"feed": inst.key, "rest": False, "websocket": False, "trade_records": 0}
        try:
            inst, info = await asyncio.wait_for(rest.metadata(inst), 25)
            result["rest"] = True
            result["size_multiplier_btc"] = str(inst.size_multiplier)
            if inst.market == "linear":
                snapshots = await asyncio.wait_for(rest.derivatives(inst, "probe"), 45)
                result["derivative_datasets"] = [r.dataset for r in snapshots]
                result["oi_basis"] = snapshots[0].payload["raw_basis"]
            url, request = subscription(inst, settings)
            async with websockets.connect(url, open_timeout=10, close_timeout=2) as ws:
                if request:
                    await ws.send(json.dumps(request))
                deadline = asyncio.get_running_loop().time() + seconds
                while asyncio.get_running_loop().time() < deadline:
                    remaining = deadline - asyncio.get_running_loop().time()
                    raw = await asyncio.wait_for(ws.recv(), remaining)
                    if raw == "pong":
                        continue
                    trades = parse_message(inst, json.loads(raw), datetime.now(UTC))
                    if trades:
                        result.update(
                            websocket=True,
                            trade_records=len(trades),
                            sample_event_time=trades[0].event_time,
                            sample_taker_side=trades[0].taker_side,
                        )
                        break
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            if hasattr(exc, "response"):
                result["http_status"] = exc.response.status_code
        return result

    try:
        return await asyncio.gather(*(one(i) for i in instruments(settings.archive_venues)))
    finally:
        await rest.close()


async def export_archive(store, start, end, output: Path, expected):
    manifest_path = output.with_name(output.name + ".manifest.json")
    exists = await asyncio.to_thread(lambda: output.exists() or manifest_path.exists())
    if exists:
        raise FileExistsError("Export refuses to overwrite an existing dataset")
    output.parent.mkdir(parents=True, exist_ok=True)
    digest, count, includes_history = hashlib.sha256(), 0, False
    with output.open("xb") as handle:
        async for row in store.scan(start, end):
            line = (
                json.dumps(serializable(row), sort_keys=True, separators=(",", ":")).encode()
                + b"\n"
            )
            handle.write(line)
            digest.update(line)
            count += 1
            includes_history |= row["source_type"].endswith("history")
    manifest = {
        "schema_version": 1,
        "start": start,
        "end": end,
        "exported_at": datetime.now(UTC),
        "rows": count,
        "sha256": digest.hexdigest(),
        "expected_instruments": [
            {"venue": i.venue, "market": i.market, "symbol": i.symbol, "quote": i.quote}
            for i in expected
        ],
        "includes_retrospective_history": includes_history,
        "notice": "Use as_received replay for causal checks; historical mode is ex-post.",
    }
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(serializable(manifest), handle, indent=2)
    return manifest


def read_export(path: Path) -> tuple[list[dict], dict]:
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("Replay input exceeds 256 MiB; export smaller explicit ranges")
    manifest = json.loads(path.with_name(path.name + ".manifest.json").read_text(encoding="utf-8"))
    digest, rows = hashlib.sha256(), []
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            row = json.loads(line)
            for key in ("event_time", "end_time", "received_at", "available_at"):
                row[key] = parse_time(row[key])
            rows.append(row)
    if digest.hexdigest() != manifest["sha256"] or len(rows) != manifest["rows"]:
        raise ValueError("Dataset hash/count differs from manifest")
    if manifest["schema_version"] != 1:
        raise ValueError("Unsupported dataset schema")
    return rows, manifest


async def execute(args):
    settings = get_settings()
    configure_logging(settings.log_level)
    expected = instruments(settings.archive_venues)
    if args.command == "coinalyze-probe":
        client, rest = CoinalyzeClient(settings), PublicRest(settings)
        try:
            source = CoinalyzeSource(settings, client)
            catalog = await source.discover()
            calibration = await source.calibrate(rest)
            catalog = source.catalog()
            end = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5)
            start = end - timedelta(minutes=3)
            checks = []
            for market in source.markets:
                for endpoint in source.datasets(market):
                    rows = await source.observations(market, endpoint, start, end)
                    checks.append(
                        {
                            "feed": market.instrument.key,
                            "endpoint": endpoint,
                            "returned": len(rows),
                            "expected_bars": 3,
                            "volume_unit": market.volume_unit,
                        }
                    )
            print(
                json.dumps(
                    serializable(
                        {
                            "catalog": catalog,
                            "calibration": calibration,
                            "funding_unit": source.funding_unit,
                            "checks": checks,
                        }
                    ),
                    indent=2,
                )
            )
        finally:
            await client.close()
            await rest.close()
        return
    if args.command == "probe":
        print(json.dumps(serializable(await probe(settings, args.seconds)), indent=2), flush=True)
        return
    if args.command == "replay":
        from btc_monitor.archive.types import Instrument

        rows, manifest = read_export(args.input)
        start, end = parse_time(args.start), parse_time(args.end)
        validate_range(start, end)
        if end - start > timedelta(days=7):
            raise ValueError("Replay jobs are bounded to 7 days")
        if parse_time(manifest["start"]) > start or parse_time(manifest["end"]) < end:
            raise ValueError("Replay range is outside exported range")
        expected = [
            Instrument(r["venue"], r["market"], r["symbol"], r["quote"])
            for r in manifest["expected_instruments"]
        ]
        summary = {
            "mode": args.mode,
            "timeframe": args.timeframe,
            "dataset_sha256": manifest["sha256"],
            "windows": 0,
            "complete_flow_windows": 0,
            "spot_buy_futures_sell_windows": 0,
            "strategy_backtest": False,
        }
        # Validate window alignment BEFORE creating an output file.
        seconds = TIMEFRAMES[args.timeframe]
        if int(start.timestamp()) % seconds or int(end.timestamp()) % seconds:
            raise ValueError("Use UTC timeframe-aligned replay boundaries")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            for window in replay_windows(
                rows, expected, start, end, args.timeframe, args.mode, args.delay_seconds
            ):
                window["dataset_sha256"] = manifest["sha256"]
                handle.write(json.dumps(serializable(window), separators=(",", ":")) + "\n")
                summary["windows"] += 1
                summary["complete_flow_windows"] += int(window["flow_complete"])
                summary["spot_buy_futures_sell_windows"] += int(window["spot_buy_futures_sell"])
        print(json.dumps(summary, indent=2), flush=True)
        return
    database = Database(settings.database_url)
    store = ArchiveStore(database)
    try:
        if args.command == "collect":
            collector = ResearchCollector(
                settings.model_copy(update={"archive_enabled": True}), store
            )
            await collector.start()
            try:
                if args.seconds:
                    await asyncio.sleep(args.seconds)
                    print(json.dumps(serializable(collector.health()), indent=2), flush=True)
                else:
                    await asyncio.Future()
            finally:
                await collector.stop()
        elif args.command == "coverage":
            print(json.dumps(serializable(await store.summary()), indent=2), flush=True)
        elif args.command == "export":
            start, end = parse_time(args.start), parse_time(args.end)
            validate_range(start, end)
            # Ten-minute lookback allows OI baseline at the start of a window.
            print(
                json.dumps(
                    serializable(
                        await export_archive(
                            store,
                            start - timedelta(minutes=10),
                            end,
                            args.output,
                            expected,
                        )
                    ),
                    indent=2,
                ),
                flush=True,
            )
        elif args.command == "backfill":
            start, end = parse_time(args.start), parse_time(args.end)
            validate_range(start, end)
            chosen = [
                i
                for i in expected
                if i.venue == args.venue and (args.market == "all" or i.market == args.market)
            ]
            if not chosen:
                raise ValueError("Requested source is outside configured archive universe")
            rest, reports = PublicRest(settings), []
            try:
                loader = HistoryLoader(rest, store)
                for inst in chosen:
                    report = await loader.prices_and_flow(inst, start, end)
                    if inst.market == "linear":
                        report.update(await loader.derivatives(inst, start, end))
                    reports.append(report)
                    print(json.dumps(report), flush=True)
            finally:
                await rest.close()
        elif args.command == "coinalyze-backfill":
            start, end = parse_time(args.start), parse_time(args.end)
            validate_range(start, end)
            client, rest = CoinalyzeClient(settings), PublicRest(settings)
            try:
                source = CoinalyzeSource(settings, client)
                await source.discover()
                await source.calibrate(rest)
                await store.append([source.metadata_observation()])
                report = await source.backfill(store, start, end, args.interval)
                print(json.dumps(serializable(report), indent=2), flush=True)
            finally:
                await client.close()
                await rest.close()
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("probe", "collect"):
        child = commands.add_parser(command)
        child.add_argument("--seconds", type=int, default=15 if command == "probe" else 0)
    commands.add_parser("coverage")
    commands.add_parser("coinalyze-probe")
    provider = commands.add_parser("coinalyze-backfill")
    provider.add_argument("--start", required=True)
    provider.add_argument("--end", required=True)
    provider.add_argument("--interval", choices=INTERVALS, default="1min")
    for command in ("backfill", "export", "replay"):
        child = commands.add_parser(command)
        child.add_argument("--start", required=True, help="UTC ISO timestamp, inclusive")
        child.add_argument("--end", required=True, help="UTC ISO timestamp, exclusive")
        if command == "backfill":
            child.add_argument(
                "--venue", required=True, choices=("bybit", "binance", "okx", "coinbase")
            )
            child.add_argument("--market", default="all", choices=("all", "spot", "linear"))
        else:
            child.add_argument("--output", type=Path, required=True)
        if command == "replay":
            child.add_argument("--input", type=Path, required=True)
            child.add_argument("--timeframe", choices=TIMEFRAMES, default="1h")
            child.add_argument(
                "--mode", choices=("as_received", "historical"), default="as_received"
            )
            child.add_argument("--delay-seconds", type=int, default=15)
    args = parser.parse_args()
    if getattr(args, "seconds", 0) < 0 or getattr(args, "delay_seconds", 0) < 0:
        parser.error("Durations must be nonnegative")
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Never print URLs/headers from requests containing optional provider credentials.
        # Validation errors contain no request credentials and should be actionable.
        detail = (
            f": {exc}" if isinstance(exc, (ValueError, FileExistsError, CoinalyzeError)) else ""
        )
        parser.exit(1, f"Archive command failed: {type(exc).__name__}{detail}\n")


if __name__ == "__main__":
    main()
