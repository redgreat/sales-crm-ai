"""健康与就绪。

/health：进程活着（不做任何依赖检查）；
/ready：数据库可达、业务表版本正确、checkpoint 结构正确、模型 Provider 可用。
"""
from __future__ import annotations

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
    ok = all(item["ok"] for item in checks.values())
    body = {"status": "ok" if ok else "not_ready", "checks": checks}
    return JSONResponse(status_code=200 if ok else 503, content=body)
