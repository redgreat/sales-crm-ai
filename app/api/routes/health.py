"""健康与就绪。

/health：进程活着（不做任何依赖检查）；
/ready：数据库可达、业务表版本正确、checkpoint 结构正确、模型 Provider 可用。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    checks: dict[str, Any] = {
        "schema": {
            "ok": bool(getattr(request.app.state, "schema_ok", False)),
            "revision": getattr(request.app.state, "schema_revision", None),
        },
        "checkpoint": {
            "ok": bool(getattr(request.app.state, "checkpoint_ok", False)),
            "version": getattr(request.app.state, "checkpoint_version", None),
        },
        "model_provider": {"ok": getattr(request.app.state, "model", None) is not None},
    }
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.worker.enabled:
        workers = getattr(request.app.state, "workers", [])
        checks["workers"] = {
            "ok": len(workers) == settings.worker.concurrency and all(not task.done() for task in workers),
            "running": sum(not task.done() for task in workers),
            "expected": settings.worker.concurrency,
        }
    stop_event = getattr(request.app.state, "stop_event", None)
    if stop_event is not None:
        checks["draining"] = {"ok": not stop_event.is_set()}
    pool = getattr(request.app.state, "pool", None)
    if pool is not None:
        try:
            async def check_database() -> None:
                async with pool.connection() as conn:
                    await conn.execute("SELECT 1")

            await asyncio.wait_for(check_database(), timeout=2.0)
            checks["database"] = {"ok": True}
        except Exception:
            checks["database"] = {"ok": False}
    ok = all(item["ok"] for item in checks.values())
    body = {"status": "ok" if ok else "not_ready", "checks": checks}
    return JSONResponse(status_code=200 if ok else 503, content=body)
