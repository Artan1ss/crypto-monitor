"""Local live market display; no signal engine or Telegram worker is started."""

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import make_url

from btc_monitor.access import MonitorAccess
from btc_monitor.archive.store import ArchiveStore
from btc_monitor.config import get_settings
from btc_monitor.db import Database
from btc_monitor.hourly.api import routes
from btc_monitor.hourly.market import HourlyMarketFeed
from btc_monitor.hourly.repository import HourlyRepository
from btc_monitor.hourly.service import HourlyMonitor

settings = get_settings().model_copy(
    update={"hourly_enabled": False, "hourly_telegram_enabled": False, "dashboard_password": None}
)
database = Database(settings.database_url)
repository = HourlyRepository(database)
monitor = HourlyMonitor(settings, repository)
local_store = (
    ArchiveStore(database)
    if make_url(settings.database_url).host in {"127.0.0.1", "localhost", "::1"}
    else None
)
live_feed = HourlyMarketFeed(settings, local_store)


@asynccontextmanager
async def lifespan(app):
    await live_feed.start()
    try:
        yield
    finally:
        await live_feed.stop()
        await monitor.stop()
        await database.close()


app = FastAPI(title="BTC 1H local review", lifespan=lifespan)
app.add_middleware(MonitorAccess, password=None)
app.include_router(
    routes(
        monitor,
        repository,
        local_reports=Path(__file__).resolve().parents[3] / ".research-data",
        live_feed=live_feed,
    )
)


if __name__ == "__main__":
    uvicorn.run("btc_monitor.hourly.preview:app", host="127.0.0.1", port=8011)
