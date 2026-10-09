from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response, status

from btc_monitor.config import get_settings
from btc_monitor.db import Database
from btc_monitor.logging import configure_logging
from btc_monitor.repository import Repository
from btc_monitor.runtime import MonitorRuntime

settings = get_settings()
configure_logging(settings.log_level)
database = Database(settings.database_url)
repository = Repository(database)
runtime = MonitorRuntime(settings, repository)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()
        await database.close()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "strategy_version": "v1", "docs": "/docs"}


@app.get("/health")
async def health(response: Response) -> dict:
    payload = runtime.health(await database.ping())
    if not payload["healthy"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload


@app.get("/ready")
async def ready(response: Response) -> dict:
    database_ok = await database.ping()
    is_ready = database_ok and runtime.status.value in {"RUNNING", "WARMING_UP"}
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": is_ready, "database": database_ok, "status": runtime.status.value}


def run() -> None:
    uvicorn.run("btc_monitor.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
