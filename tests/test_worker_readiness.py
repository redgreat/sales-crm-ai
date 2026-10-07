"""就绪检查在后台 worker 意外退出时拒绝流量。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes.health import router


@pytest.mark.asyncio
async def test_ready_fails_when_worker_stops() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.schema_ok = True
    app.state.checkpoint_ok = True
    app.state.model = object()
    app.state.settings = SimpleNamespace(worker=SimpleNamespace(enabled=True, concurrency=1))
    worker = asyncio.create_task(asyncio.sleep(60))
    app.state.workers = [worker]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/ready")).status_code == 200
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            response = await client.get("/ready")
            assert response.status_code == 503
            assert response.json()["checks"]["workers"] == {
                "ok": False, "running": 0, "expected": 1,
            }
            assert (await client.get("/health")).status_code == 200
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_ready_fails_during_shutdown() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.schema_ok = True
    app.state.checkpoint_ok = True
    app.state.model = object()
    app.state.stop_event = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/ready")).status_code == 200
        app.state.stop_event.set()
        response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["draining"] == {"ok": False}


@pytest.mark.asyncio
async def test_ready_fails_when_database_is_unreachable() -> None:
    class BrokenPool:
        def connection(self):
            raise ConnectionError("database unavailable")

    app = FastAPI()
    app.include_router(router)
    app.state.schema_ok = True
    app.state.checkpoint_ok = True
    app.state.model = object()
    app.state.pool = BrokenPool()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["database"] == {"ok": False}
