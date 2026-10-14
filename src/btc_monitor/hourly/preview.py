"""Local research UI only: no V1 collector or Telegram worker is started."""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from btc_monitor.access import MonitorAccess
from btc_monitor.config import get_settings
from btc_monitor.db import Database
from btc_monitor.hourly.api import routes
from btc_monitor.hourly.repository import HourlyRepository
from btc_monitor.hourly.service import HourlyMonitor

settings = get_settings().model_copy(
    update={"hourly_enabled": False, "hourly_telegram_enabled": False, "dashboard_password": None}
)
database = Database(settings.database_url)
repository = HourlyRepository(database)
monitor = HourlyMonitor(settings, repository)


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await monitor.stop()
        await database.close()


app = FastAPI(title="BTC 1H local review", lifespan=lifespan)
app.add_middleware(MonitorAccess, password=None)
app.include_router(routes(monitor, repository))


if __name__ == "__main__":
    uvicorn.run("btc_monitor.hourly.preview:app", host="127.0.0.1", port=8011)
