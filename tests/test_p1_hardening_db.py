"""P1 加固的真实数据库验证（结果版本透出 / 入参上限 / 错误脱敏落库）。

与 tests/test_p1_hardening.py 的分工：那一层是纯逻辑单测（快、CI 必跑），
这一层证明加固在"真的写库、真的过 HTTP"时依然生效——尤其是错误脱敏，
因为泄漏只发生在 ai_runs.error / status_history 的落库内容里。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.api.main import create_app
from app.auth import OperatorContext, sign_request
from app.config import Settings
from app.persistence import runs as runs_repo
from app.runtime.executor import Executor

pytestmark = pytest.mark.realpg

SECRET = "sk-abcdef1234567890"


def _dev_settings(db_url: str) -> Settings:
    return Settings(
        environment="dev",
        database_url=db_url,
        model={"provider": "stub"},
        auth={"service_secret": "dev-secret", "service_key_id": "crm-ai"},
        worker={
            "enabled": True,
            "concurrency": 2,
            "poll_interval_seconds": 0.05,
            "lease_seconds": 120,
            "backoff_base_seconds": 0.2,
        },
    )


@pytest_asyncio.fixture
async def dev_api(db_url: str):
    settings = _dev_settings(db_url)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai") as client:
            yield client, settings


def _signed(settings: Settings, method: str, path: str, body: bytes, user_id: str = "u1") -> dict[str, str]:
    return sign_request(
        secret=settings.auth.service_secret,
        key_id=settings.auth.service_key_id,
        method=method,
        path=path,
        body=body,
        operator=OperatorContext(user_id=user_id, user_name="测试用户"),
    )


async def _post(client: httpx.AsyncClient, settings: Settings, path: str, payload: dict,
                user_id: str = "u1") -> httpx.Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _signed(settings, "POST", path, body, user_id)
    headers["Content-Type"] = "application/json"
    return await client.post(path, content=body, headers=headers)


async def _get(client: httpx.AsyncClient, settings: Settings, path: str, user_id: str = "u1") -> httpx.Response:
    # 签名只覆盖路径（服务端按 request.url.path 校验），query 不能进签名串
    return await client.get(path, headers=_signed(settings, "GET", path.split("?")[0], b"", user_id))


async def _wait_status(client, settings, run_id: str, statuses: set[str], timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = await _get(client, settings, f"/api/v1/runs/{run_id}")
        body = response.json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"等待状态超时: {statuses}, 最后: {body}")


GOOD_TEXT = "客户：ACME公司\n任务：电话回访\n负责人：张三\n2026-10-01"


def _run_kwargs(**overrides) -> dict:
    kwargs = dict(
        idempotency_key=f"p1-{uuid.uuid4().hex}",
        capability="communication.extract",
        input_payload={"text": GOOD_TEXT},
        operator={"user_id": "u1"},
        thread_id=None,
        conversation_id=None,
        graph_version="extract@1",
        prompt_version="extract-prompt@1",
        max_attempts=1,
    )
    kwargs.update(overrides)
    return kwargs


class _ExplodingGraph:
    """模拟供应商异常：报文里带密钥——这是最典型的泄漏来源。"""

    def __init__(self, exc: Exception):
        self.exc = exc

    async def ainvoke(self, payload, config=None):
        raise self.exc

    async def aget_state(self, config):
        return SimpleNamespace(next=None)


class _SucceedingGraph:
    """返回可持久化的结果，用于触发"落库阶段"崩溃。"""

    async def ainvoke(self, payload, config=None):
        return {"result": {"summary": "ok", "candidates": {"activities": [], "tasks": []}}}

    async def aget_state(self, config):
        return SimpleNamespace(next=None)


async def test_run_api_exposes_result_version(dev_api):
    """结果版本必须随 Run 透出：CRM 靠它判断"AI 结果是否已经变了"。"""
    client, settings = dev_api
    response = await _post(client, settings, "/api/v1/runs", {
        "capability": "communication.extract",
        "input": {"text": GOOD_TEXT},
        "idempotency_key": "p1-version-check",
    })
    assert response.status_code == 202
    created = response.json()
    assert created["result_version"] == 0  # 未完成时是 0，不是缺失
    body = await _wait_status(client, settings, created["run_id"], {"succeeded"})
    assert body["result_version"] == 1


async def test_create_run_rejects_oversized_input_before_queueing(dev_api):
    """超限输入必须 413，且不能入队（否则会白跑一次 attempt 并把超长原文写进库）。"""
    client, settings = dev_api
    response = await _post(client, settings, "/api/v1/runs", {
        "capability": "communication.extract",
        "input": {"text": "甲" * 20001},
    })
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    listed = await _get(client, settings, "/api/v1/runs?limit=50")
    assert listed.json()["count"] == 0


async def test_facts_over_limit_rejected(dev_api):
    client, settings = dev_api
    facts = [{"id": f"f{i}", "label": "x"} for i in range(201)]
    response = await _post(client, settings, "/api/v1/runs", {
        "capability": "business.qa",
        "input": {"question": "客户最近动态？", "facts": facts,
                  "scope": {"subject_type": "customer", "subject_id": "c1"}},
    })
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_graph_error_is_redacted_before_persist(db_pool):
    """图内供应商异常：密钥不能进 ai_runs.error / status_history。"""
    graph = _ExplodingGraph(RuntimeError(f"供应商返回 401: api_key={SECRET}"))
    executor = Executor(db_pool, {"communication.extract": graph}, lease_seconds=60,
                        max_attempts_default=1, backoff_base_seconds=0.1)
    run, _ = await runs_repo.create_run(db_pool, **_run_kwargs())
    await executor.run_batch()
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "failed"
    persisted = json.dumps(final["error"], ensure_ascii=False) + json.dumps(
        final["status_history"], ensure_ascii=False
    )
    assert SECRET not in persisted
    assert final["error"]["code"] == "GRAPH_FAILED"


async def test_executor_crash_is_redacted_before_persist(db_pool, monkeypatch):
    """落库阶段崩溃（非图异常）：只保留异常类型，不回显原始报文。"""
    async def boom(*args, **kwargs):
        raise RuntimeError(f"数据库连接失败 url=https://db.internal/x?token={SECRET}")

    monkeypatch.setattr(runs_repo, "complete_run", boom)
    executor = Executor(db_pool, {"communication.extract": _SucceedingGraph()}, lease_seconds=60,
                        max_attempts_default=1, backoff_base_seconds=0.1)
    run, _ = await runs_repo.create_run(db_pool, **_run_kwargs())
    await executor.run_batch()
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "failed"
    assert final["error"]["code"] == "EXECUTOR_CRASH"
    persisted = json.dumps(final["error"], ensure_ascii=False) + json.dumps(
        final["status_history"], ensure_ascii=False
    )
    assert SECRET not in persisted
    assert "db.internal" not in persisted
    assert "RuntimeError" in final["error"]["message"]
