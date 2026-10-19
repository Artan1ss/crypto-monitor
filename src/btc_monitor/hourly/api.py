import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from btc_monitor.archive.types import serializable
from btc_monitor.hourly.repository import alert_json
from btc_monitor.hourly.review import review_bars
from btc_monitor.hourly.service import hour

WEB = Path(__file__).parent / "web"


@asynccontextmanager
async def database_read():
    try:
        async with asyncio.timeout(15):
            yield
    except (OSError, SQLAlchemyError, TimeoutError):
        # Never return database exception text: it can include connection credentials or SQL.
        raise HTTPException(
            503,
            "数据库暂不可用。若使用本地面板，请检查 Docker / PostgreSQL 是否启动；"
            "本地面板不会自动连接 Railway。可先读取本地快照（非实时）。",
        ) from None


def routes(monitor, repo, local_reports=None, live_feed=None):
    router = APIRouter()

    @router.get("/hourly/sweep-research", include_in_schema=False)
    async def sweep_research():
        if live_feed is None or local_reports is None:
            raise HTTPException(404, "扫点验证仅在本机预览中可用")
        from btc_monitor.hourly.sweep_research import dashboard_payload, replay

        payload = await live_feed.view(168)
        report = await asyncio.to_thread(replay, payload)
        return await asyncio.to_thread(dashboard_payload, report)

    @router.get("/hourly/market")
    async def market(hours: int = Query(72, ge=1, le=168)):
        if live_feed is None:
            raise HTTPException(404, "此入口未启用独立实时行情，仍可查看数据库归档")
        try:
            return await live_feed.view(hours)
        except Exception:
            raise HTTPException(
                502, "实时价格源暂不可用；不会用旧快照冒充实时行情，请稍后重试"
            ) from None

    @router.get("/hourly/local-snapshot", include_in_schema=False)
    async def local_snapshot():
        # Explicitly wired only by loopback preview; no arbitrary path or production exposure.
        if local_reports is None:
            raise HTTPException(404, "本地快照仅在本机预览中可用")
        directory = Path(local_reports).resolve()
        files = sorted(directory.glob("h1-review-*-live-cloud.json.gz"))
        if not files:
            raise HTTPException(404, "未找到已保存的云端快照，请导入离线复盘文件")
        path = files[-1].resolve()
        if path.parent != directory or path.stat().st_size > 50 * 1024 * 1024:
            raise HTTPException(404)
        return FileResponse(path, media_type="application/gzip", filename=path.name)

    @router.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return FileResponse(WEB / "index.html")

    @router.get("/dashboard/assets/{name}", include_in_schema=False)
    async def asset(name: str):
        if name not in {"app.js", "app.css", "viewport.js", "chart.js"}:
            raise HTTPException(404)
        return FileResponse(WEB / name)

    @router.get("/hourly/health")
    async def health():
        result = monitor.health()
        if live_feed is not None:
            result = {**result, "live_market": live_feed.health()}
        if local_reports is not None:
            try:
                connected = await asyncio.wait_for(repo.db.ping(), timeout=3)
            except (OSError, SQLAlchemyError, TimeoutError):
                connected = False
            result = {**result, "database": {"scope": "local_preview", "connected": connected}}
        return result

    @router.get("/hourly/bars")
    async def bars(hours: int = Query(72, ge=1, le=168)):
        now = datetime.now(UTC)
        async with database_read():
            return await review_bars(
                repo, monitor.settings.hourly_source, hour(now) - timedelta(hours=hours), now
            )

    @router.get("/hourly/signals")
    async def signals(limit: int = Query(200, ge=1, le=500), hours: int = Query(72, ge=1, le=168)):
        now = datetime.now(UTC)
        async with database_read():
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
            revisions = await repo.outcome_revisions(signal_id)
            ids = set(row.snapshot["input_ids"])
            for outcome in [row.outcomes] + [r["result"] for r in revisions]:
                for w in (outcome or {}).get("windows", {}).values():
                    ids.update(w.get("input_ids", []))
                    ids.update(w.get("quality_gap_ids", []))
                    if w.get("boundary_input_id"):
                        ids.add(w["boundary_input_id"])
            raw = await repo.raw_inputs(sorted(ids))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse(
            serializable(
                {
                    "signal": alert_json(row, True),
                    "raw_inputs": raw,
                    "outcome_revisions": revisions,
                    "revision_limit": 200,
                }
            ),
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
