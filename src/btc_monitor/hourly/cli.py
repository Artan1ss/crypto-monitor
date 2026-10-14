"""Export offline charts/evidence without exposing a cloud HTTP service."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from btc_monitor.archive.types import serializable
from btc_monitor.config import get_settings
from btc_monitor.db import Database
from btc_monitor.hourly.repository import HourlyRepository, alert_json
from btc_monitor.hourly.service import HourlyMonitor, hour


def save_export(output, payload):
    encoded = json.dumps(serializable(payload), ensure_ascii=False)
    path = Path(output).resolve()
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
    return str(path)


async def execute(args):
    settings = get_settings()
    database = Database(settings.database_url)
    repo = HourlyRepository(database)
    monitor = HourlyMonitor(settings, repo)
    try:
        now = datetime.now(UTC)
        end = hour(now - timedelta(seconds=settings.hourly_decision_delay_seconds))
        if args.command == "replay":
            async with repo.lease() as lease:
                if lease is None:
                    raise RuntimeError("Hourly writer busy; retry after current cycle")
                await monitor.rebuild(end - timedelta(hours=args.hours), end, lease)
                await monitor.update_outcomes(now)
            print(json.dumps({"state": "complete", "notifications": False}))
        else:
            payload = {
                "schema": "btc-hourly-review-v1",
                "exported_at": now,
                "bars": await repo.bars(
                    settings.hourly_source, end - timedelta(hours=args.hours), now
                ),
                "signals": [
                    alert_json(r, True)
                    for r in await repo.alerts(
                        settings.hourly_source, 500, end - timedelta(hours=args.hours), now
                    )
                ],
                "health": {"source": settings.hourly_source, "offline": True},
                "notice": (
                    "Offline evidence, not live health. Raw inputs use cloud immutable IDs; "
                    "individual API exports include raw records."
                ),
            }
            path = await asyncio.to_thread(save_export, args.output, payload)
            print(
                json.dumps(
                    {
                        "file": str(path),
                        "bars": len(payload["bars"]),
                        "signals": len(payload["signals"]),
                    }
                )
            )
    finally:
        await monitor.stop()
        await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("replay", "export"):
        item = sub.add_parser(name)
        item.add_argument("--hours", type=int, choices=range(30, 169), default=72)
        if name == "export":
            item.add_argument("--output", required=True)
    asyncio.run(execute(parser.parse_args()))


if __name__ == "__main__":
    main()
