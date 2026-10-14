from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.repository import alert_json
from btc_monitor.hourly.service import hour

WEB = Path(__file__).parent / "web"


def routes(monitor, repo):
    router = APIRouter()

    @router.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return FileResponse(WEB / "index.html")

    @router.get("/dashboard/assets/{name}", include_in_schema=False)
    async def asset(name: str):
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return FileResponse(WEB / name)

    @router.get("/hourly/health")
    async def health():
        return monitor.health()

    @router.get("/hourly/bars")
    async def bars(hours: int = Query(72, ge=1, le=168)):
        now = datetime.now(UTC)
        return await repo.bars(
            monitor.settings.hourly_source, hour(now) - timedelta(hours=hours), now
        )

    @router.get("/hourly/signals")
    async def signals(limit: int = Query(200, ge=1, le=500), hours: int = Query(72, ge=1, le=168)):
        now = datetime.now(UTC)
        rows = await repo.alerts(
            monitor.settings.hourly_source, limit, hour(now) - timedelta(hours=hours), now
        )
        return serializable([alert_json(r) for r in rows])

    @router.get("/hourly/signals/{signal_id}")
    async def detail(signal_id: UUID):
        row = await repo.alert(signal_id)
        if not row:
            raise HTTPException(404, "Signal not found")
        return JSONResponse(serializable(alert_json(row, True)))

    @router.get("/hourly/signals/{signal_id}/export")
    async def export(signal_id: UUID):
        row = await repo.alert(signal_id)
        if not row:
            raise HTTPException(404, "Signal not found")
        try:
            raw = await repo.raw_inputs(row.snapshot["input_ids"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse(
            serializable({"signal": alert_json(row, True), "raw_inputs": raw}),
            headers={"Content-Disposition": f'attachment; filename="h1-{signal_id}.json"'},
        )

    @router.post("/hourly/replay")
    async def replay(request: Request, hours: int = Query(72, ge=30, le=168)):
        # Same-origin JSON-only POST: no cross-site form-triggered expensive replay.
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/").split("://")[-1] != request.headers.get("host"):
            raise HTTPException(403, "Same origin required")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "JSON request required")
        if not await monitor.request_replay(hours):
            raise HTTPException(409, "Replay already running")
        return JSONResponse({"state": "queued", "notifications": False}, status_code=202)

    return router
