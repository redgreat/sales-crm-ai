"""API 契约测试（真实 PostgreSQL + Stub 模型 + 内置 worker）。

覆盖：健康/就绪、Run 全生命周期、幂等、跨用户拒绝、取消、
等待-恢复多轮、会话绑定、签名认证与防重放、usage 不落响应。
"""
from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest
import pytest_asyncio

from app.api.main import create_app
from app.auth import OperatorContext, sign_request
from app.config import Settings

pytestmark = pytest.mark.realpg


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


async def _post(client: httpx.AsyncClient, settings: Settings, path: str, payload: dict, user_id: str = "u1") -> httpx.Response:
    import json

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _signed(settings, "POST", path, body, user_id)
    headers["Content-Type"] = "application/json"
    return await client.post(path, content=body, headers=headers)


async def _get(client: httpx.AsyncClient, settings: Settings, path: str, user_id: str = "u1") -> httpx.Response:
    return await client.get(path, headers=_signed(settings, "GET", path, b"", user_id))


async def _wait_status(client, settings, run_id: str, statuses: set[str], timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await _get(client, settings, f"/api/v1/runs/{run_id}")
        body = response.json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"等待状态超时: {statuses}, 最后: {body}")


GOOD_TEXT = "客户：ACME公司\n任务：电话回访\n负责人：张三\n2026-10-01"
MISSING_TEXT = "[[缺日期]]\n任务：电话跟进\n客户：ACME\n负责人：张三"


async def test_health_and_ready(dev_api):
    client, settings = dev_api
    assert (await client.get("/health")).status_code == 200
    ready = await client.get("/ready")
    assert ready.status_code == 200
    checks = ready.json()["checks"]
    assert checks["schema"]["ok"] and checks["checkpoint"]["ok"]


async def test_unsigned_request_rejected_in_dev(dev_api):
    client, _ = dev_api
    response = await client.post("/api/v1/runs", json={})
    assert response.status_code == 401


async def test_replayed_request_rejected(dev_api):
    """同一签名头重放（相同 nonce）第二次被拒。"""
    client, settings = dev_api
    payload = {"capability": "communication.extract", "input": {"text": GOOD_TEXT},
               "idempotency_key": f"replay-{uuid.uuid4().hex}"}
    import json

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _signed(settings, "POST", "/api/v1/runs", body)
    headers["Content-Type"] = "application/json"
    first = await client.post("/api/v1/runs", content=body, headers=headers)
    assert first.status_code == 202
    second = await client.post("/api/v1/runs", content=body, headers=headers)
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_run_lifecycle_with_stub(dev_api):
    client, settings = dev_api
    payload = {"capability": "communication.extract", "input": {"text": GOOD_TEXT},
               "idempotency_key": f"e2e-{uuid.uuid4().hex}"}
    response = await _post(client, settings, "/api/v1/runs", payload)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["idempotent_replay"] is False

    final = await _wait_status(client, settings, body["run_id"], {"succeeded"})
    task = final["result"]["candidates"]["tasks"][0]
    assert task["title"] == "电话回访" and task["due_date"] == "2026-10-01"
    # 供应商 usage 不出现在结果里
    result_text = str(final["result"]).lower()
    assert "usage" not in result_text and "token" not in result_text

    # 幂等重放：返回原 Run
    replay = await _post(client, settings, "/api/v1/runs", payload)
    assert replay.json()["run_id"] == body["run_id"]
    assert replay.json()["idempotent_replay"] is True


async def test_unknown_capability_rejected(dev_api):
    client, settings = dev_api
    response = await _post(client, settings, "/api/v1/runs",
                           {"capability": "sql.free", "input": {"text": "x"},
                            "idempotency_key": f"cap-{uuid.uuid4().hex}"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CAPABILITY_UNKNOWN"


async def test_cross_user_access_denied(dev_api):
    client, settings = dev_api
    response = await _post(client, settings, "/api/v1/runs",
                           {"capability": "communication.extract", "input": {"text": GOOD_TEXT},
                            "idempotency_key": f"owner-{uuid.uuid4().hex}"})
    run_id = response.json()["run_id"]
    other = await _get(client, settings, f"/api/v1/runs/{run_id}", user_id="attacker")
    assert other.status_code == 403


async def test_cancel_run(dev_api):
    client, settings = dev_api
    response = await _post(client, settings, "/api/v1/runs",
                           {"capability": "communication.extract", "input": {"text": GOOD_TEXT},
                            "idempotency_key": f"cancel-{uuid.uuid4().hex}"})
    run_id = response.json()["run_id"]
    cancel = await _post(client, settings, f"/api/v1/runs/{run_id}/cancel", {})
    assert cancel.status_code == 200
    final = await _get(client, settings, f"/api/v1/runs/{run_id}")
    assert final.json()["status"] in {"cancelled", "succeeded"}  # 已在途的可能先完成


async def test_missing_input_wait_resume_and_duplicate_rejection(dev_api):
    client, settings = dev_api
    response = await _post(client, settings, "/api/v1/runs",
                           {"capability": "communication.extract", "input": {"text": MISSING_TEXT},
                            "idempotency_key": f"wait-{uuid.uuid4().hex}"})
    run_id = response.json()["run_id"]
    waiting = await _wait_status(client, settings, run_id, {"waiting_input"})

    # 版本不匹配（旧页面）拒绝
    wrong = await _post(client, settings, f"/api/v1/runs/{run_id}/resume",
                        {"values": {"text": "日期：2026-10-02"}, "state_version": 999})
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "STATE_VERSION_CONFLICT"

    ok = await _post(client, settings, f"/api/v1/runs/{run_id}/resume",
                     {"values": {"text": "日期：2026-10-02"}, "state_version": 1})
    assert ok.status_code == 202
    final = await _wait_status(client, settings, run_id, {"succeeded"})
    assert final["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-02"

    # 重复 resume：已被消费，拒绝
    duplicate = await _post(client, settings, f"/api/v1/runs/{run_id}/resume",
                            {"values": {"text": "日期：2026-10-03"}, "state_version": 1})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "RUN_NOT_RESUMABLE"


async def test_conversation_multi_turn_flow(dev_api):
    client, settings = dev_api
    created = await _post(client, settings, "/api/v1/conversations", {"subject_type": "customer", "subject_id": "C001"})
    assert created.status_code == 201
    conversation_id = created.json()["conversation_id"]

    # 第一轮：缺日期 → 等待
    msg1 = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                       {"text": MISSING_TEXT, "idempotency_key": f"m1-{uuid.uuid4().hex}"})
    assert msg1.status_code == 202
    run_id = msg1.json()["run_id"]
    await _wait_status(client, settings, run_id, {"waiting_input"})

    pending = await _get(client, settings, f"/api/v1/conversations/{conversation_id}/pending")
    pending_body = pending.json()
    assert pending_body["waiting"] and pending_body["state_version"] == 1
    assert any(q["field"] == "due_date" for q in pending_body["questions"])

    # 第二轮：直接发消息即恢复等待 Run
    msg2 = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                       {"text": "日期：2026-10-06", "idempotency_key": f"m2-{uuid.uuid4().hex}"})
    assert msg2.json()["mode"] == "resume"
    final = await _wait_status(client, settings, run_id, {"succeeded"})
    assert final["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-06"

    messages = await _get(client, settings, f"/api/v1/conversations/{conversation_id}/messages")
    roles = [m["role"] for m in messages.json()["messages"]]
    assert roles.count("user") == 2 and roles.count("assistant") == 1

    # 跨用户：别人的会话不可见
    other = await _get(client, settings, f"/api/v1/conversations/{conversation_id}/messages", user_id="attacker")
    assert other.status_code == 403

    # 结束会话：不能再发消息
    close = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/close", {})
    assert close.status_code == 200
    after = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                        {"text": "再聊一句", "idempotency_key": f"m3-{uuid.uuid4().hex}"})
    assert after.status_code == 422


async def test_empty_text_rejected(dev_api):
    client, settings = dev_api
    response = await _post(client, settings, "/api/v1/runs",
                           {"capability": "communication.extract", "input": {"text": "  "},
                            "idempotency_key": f"empty-{uuid.uuid4().hex}"})
    assert response.status_code == 422
