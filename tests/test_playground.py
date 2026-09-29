"""playground 联调代理测试：免签名访问、固定联调身份、未启用时 404。"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
import pytest_asyncio

from app.api.main import create_app
from app.config import Settings

pytestmark = pytest.mark.realpg


def _settings(db_url: str, *, playground_enabled: bool) -> Settings:
    return Settings(
        environment="dev",
        database_url=db_url,
        model={"provider": "stub"},
        auth={"service_secret": "dev-secret", "service_key_id": "crm-ai"},
        worker={"enabled": True, "concurrency": 1, "poll_interval_seconds": 0.05,
                "lease_seconds": 120, "backoff_base_seconds": 0.2},
        api={"playground": {"enabled": playground_enabled}},
    )


@pytest_asyncio.fixture
async def playground_api(db_url: str):
    app = create_app(_settings(db_url, playground_enabled=True))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai") as client:
            yield client


async def _wait_succeeded(client: httpx.AsyncClient, run_id: str) -> dict:
    for _ in range(100):
        response = await client.get(f"/playground/api/v1/runs/{run_id}")
        body = response.json()
        if body["status"] == "succeeded":
            return body
        await __import__("asyncio").sleep(0.1)
    raise AssertionError(f"Run 未完成: {body}")


async def test_playground_proxies_unsigned_request(playground_api):
    """浏览器无签名请求 → 代理注入固定联调身份 → Run 正常创建并完成。"""
    response = await playground_api.post(
        "/playground/api/v1/runs",
        json={
            "capability": "communication.extract",
            "input": {"text": "客户：ACME\n任务：回访\n负责人：张三\n2026-10-01"},
            "idempotency_key": f"pg-{uuid.uuid4().hex}",
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"

    final = await _wait_succeeded(playground_api, body["run_id"])
    assert final["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-01"


async def test_playground_health_and_ready_passthrough(playground_api):
    health = await playground_api.get("/playground/health")
    assert health.status_code == 200
    ready = await playground_api.get("/playground/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ok"


async def test_playground_disabled_returns_404(db_url: str):
    app = create_app(_settings(db_url, playground_enabled=False))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai") as client:
            response = await client.post("/playground/api/v1/runs", json={})
            assert response.status_code == 404
