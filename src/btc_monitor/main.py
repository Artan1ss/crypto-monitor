from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response, status

from btc_monitor.access import MonitorAccess
from btc_monitor.archive.service import ResearchCollector
from btc_monitor.archive.store import ArchiveStore
from btc_monitor.config import get_settings
from btc_monitor.db import Database
from btc_monitor.hourly.api import routes
from btc_monitor.hourly.repository import HourlyRepository
from btc_monitor.hourly.service import HourlyMonitor
from btc_monitor.logging import configure_logging
from btc_monitor.repository import Repository
from btc_monitor.runtime import MonitorRuntime
from btc_monitor.squeeze.repository import SqueezeRepository
from btc_monitor.squeeze.service import SqueezeMonitor
from btc_monitor.archive.types import serializable

settings = get_settings()
configure_logging(settings.log_level)
database = Database(settings.database_url)
repository = Repository(database)
runtime = MonitorRuntime(settings, repository)
archive_store = ArchiveStore(database)
squeeze_repository = SqueezeRepository(database)
squeeze_runtime = SqueezeMonitor(settings, squeeze_repository, archive_store)
archive_runtime = ResearchCollector(settings, archive_store, squeeze_monitor=squeeze_runtime)
hourly_repository = HourlyRepository(database)
hourly_runtime = HourlyMonitor(settings, hourly_repository)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    await archive_runtime.start()
    await hourly_runtime.start()
    try:
        yield
    finally:
        try:
            try:
                await hourly_runtime.stop()
            finally:
                await archive_runtime.stop()
        finally:
            try:
                await runtime.stop()
            finally:
                await squeeze_runtime.stop()
                await database.close()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(MonitorAccess, password=settings.dashboard_password)
app.include_router(routes(hourly_runtime, hourly_repository))


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "strategy_version": "v1", "docs": "/docs"}


@app.get("/health")
async def health(response: Response) -> dict:
    payload = runtime.health(await database.ping())
    payload["squeeze"] = squeeze_runtime.health()
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


@app.get("/signals/{signal_id}/debug")
async def signal_debug(signal_id: UUID) -> dict:
    row = await repository.get_signal(signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return {
        "id": str(row.id),
        "debug_snapshot": row.debug_snapshot,
        "legacy_features": row.features if row.debug_snapshot is None else None,
    }


@app.get("/signals/{signal_id}/outcomes")
async def signal_outcomes(signal_id: UUID) -> dict:
    row = await repository.get_signal(signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return {"id": str(row.id), "tracking_complete": row.tracking_complete, "outcomes": row.outcomes}


@app.get("/signals")
async def signals(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    rows = await repository.recent_signals(limit)
    return [
        {
            "id": str(r.id),
            "candle_start": r.signal_time,
            "generated_at": r.created_at,
            "direction": r.direction,
            "score": r.score,
            "strength": r.strength,
            "entry_reference": r.entry_reference,
            "telegram_sent": r.telegram_sent,
            "tracking_complete": r.tracking_complete,
            "snapshot_origin": (r.debug_snapshot or {}).get("snapshot_origin"),
        }
        for r in rows
    ]


@app.get("/archive/health")
async def archive_health() -> dict:
    return archive_runtime.health()


@app.get("/squeeze/health")
async def squeeze_health():
    return {**squeeze_runtime.health(),"delivery_counts":await squeeze_repository.counts()}


@app.get("/squeeze/signals")
async def squeeze_signals(limit: int = Query(100,ge=1,le=500)):
    rows=await squeeze_repository.alerts(limit)
    return serializable([{"id":r.id,"event_key":r.event_key,"generated_at":r.generated_at,
        "bar_start":r.bar_start,"direction":r.direction,"phase":r.phase,"origin":r.origin,
        "delivery_state":r.delivery_state,"delivery_id":r.delivery_id,"delivery_at":r.delivery_at,
        "confidence":r.payload["confidence"],"divergence_type":r.payload["divergence_type"]} for r in rows])


@app.get("/squeeze/signals/{identity}/export")
async def squeeze_export(identity: UUID):
    row=await squeeze_repository.alert(identity)
    if row is None:
        raise HTTPException(404,"Squeeze signal not found")
    inputs=set(row.payload["snapshot"].get("raw_input_fingerprints",[]))
    metadata=row.payload["snapshot"].get("unit_metadata_fingerprint")
    if metadata:
        inputs.add(metadata)
    return serializable({"id":row.id,"event_key":row.event_key,"payload":row.payload,
        "delivery_state":row.delivery_state,"delivery_id":row.delivery_id,"delivery_at":row.delivery_at,
        "raw_inputs":await squeeze_repository.raw(sorted(inputs))})


@app.get("/archive/coverage")
async def archive_coverage() -> list[dict]:
    return await archive_store.summary()


def run() -> None:
    uvicorn.run("btc_monitor.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
