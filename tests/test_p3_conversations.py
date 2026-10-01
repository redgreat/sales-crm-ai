"""P3 会话多轮收口测试（真实 PostgreSQL + Stub 模型 + 内置 worker）。

覆盖实施计划 P3 的三个缺口：

1. 消息窗口：历史装配取「最近 N 条」而不是「最早 N 条」；
   支持按 seq 分页（移动端收起/刷新恢复场景拉增量）。
2. 每轮独立 Run：同会话连发消息必须各起新 Run（旧实现固定幂等键
   `conv:{id}:{user}` 会让第二条消息重放第一个 Run，多轮对话完全失效）；
   客户端同 key 重试幂等重放原 Run 且消息不重复落库（M-07）。
3. 会话生命周期：TTL 过期标记 expired（M-01 红线：会话过期绝不改变候选
   业务状态）；closed/expired 会话在 messages/runs/resume 所有入口一致拒绝。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx
import pytest
import pytest_asyncio

from app.api.main import create_app
from app.auth import OperatorContext, sign_request
from app.config import Settings
from app.persistence import conversations as conversations_repo
from app.persistence import runs as runs_repo
from app.persistence.pool import open_pool

pytestmark = pytest.mark.realpg

MISSING_TEXT = "[[缺日期]]\n任务：电话跟进\n客户：ACME\n负责人：张三"


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
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _signed(settings, "POST", path, body, user_id)
    headers["Content-Type"] = "application/json"
    return await client.post(path, content=body, headers=headers)


async def _get(client: httpx.AsyncClient, settings: Settings, path: str, user_id: str = "u1", *, url: str | None = None) -> httpx.Response:
    """path 参与签名（不含 query）；url 是实际请求地址（可带 query）。"""
    return await client.get(url or path, headers=_signed(settings, "GET", path, b"", user_id))


async def _wait_status(client, settings, run_id: str, statuses: set[str], timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await _get(client, settings, f"/api/v1/runs/{run_id}")
        body = response.json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"等待 {run_id} 进入 {statuses} 超时")


async def _new_conversation(client, settings, subject_id: str = "C001") -> str:
    created = await _post(
        client, settings, "/api/v1/conversations",
        {"subject_type": "customer", "subject_id": subject_id},
    )
    assert created.status_code == 201
    return created.json()["conversation_id"]


# ---------------------------------------------------------------------------
# 1. 消息窗口：最近 N 条 + 分页
# ---------------------------------------------------------------------------


async def test_list_messages_returns_most_recent_window(db_pool):
    """窗口语义：取最近的 N 条（旧实现 ORDER BY seq LIMIT 取的是最早 N 条）。"""
    conv = await conversations_repo.create_conversation(db_pool, crm_user_id="u1")
    conversation_id = conv["conversation_id"]
    for index in range(1, 151):
        await conversations_repo.append_message(
            db_pool, conversation_id=conversation_id, role="user", content=f"消息{index}"
        )

    window = await conversations_repo.list_messages(db_pool, conversation_id, limit=100)
    seqs = [m["seq"] for m in window]
    assert len(seqs) == 100
    assert seqs == sorted(seqs)  # 输出仍按时间正序
    assert seqs[0] == 51 and seqs[-1] == 150  # 最近 100 条，而不是 1..100


async def test_list_messages_before_seq_pagination(db_url):
    """分页：before_seq 返回该 seq 之前的更早消息（移动端向上翻历史）。"""
    settings = _dev_settings(db_url)
    async with open_pool(settings) as pool:
        conv = await conversations_repo.create_conversation(pool, crm_user_id="u1")
        conversation_id = conv["conversation_id"]
        for index in range(1, 41):
            await conversations_repo.append_message(
                pool, conversation_id=conversation_id, role="user", content=f"消息{index}"
            )
        older = await conversations_repo.list_messages(
            pool, conversation_id, limit=20, before_seq=21
        )
        seqs = [m["seq"] for m in older]
        assert seqs == list(range(1, 21))


async def test_message_list_api_supports_window(db_url):
    """GET /messages 暴露窗口与分页参数，默认返回最近窗口。"""
    settings = _dev_settings(db_url)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai") as client:
            conversation_id = await _new_conversation(client, settings)
            for index in range(1, 31):
                await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                            {"text": f"消息{index}", "idempotency_key": f"k{index}-{uuid.uuid4().hex}"})
            page = await _get(
                client, settings,
                f"/api/v1/conversations/{conversation_id}/messages",
                url=f"/api/v1/conversations/{conversation_id}/messages?limit=10",
            )
            body = page.json()
            seqs = [m["seq"] for m in body["messages"]]
            # 30 条 user 消息 + worker 异步落的 assistant 消息交错；
            # 窗口断言：恰好 10 条、时间正序、且覆盖到最新消息（seq ≥ 30）
            assert len(seqs) == 10 and seqs == sorted(seqs) and seqs[-1] >= 30
            assert body["window"] == {"limit": 10, "before_seq": None}


# ---------------------------------------------------------------------------
# 2. 每轮独立 Run + 消息幂等
# ---------------------------------------------------------------------------


async def test_each_message_starts_new_run_without_client_key(dev_api):
    """不带幂等键的每条消息必须各起新 Run（固定键重放缺陷回归）。"""
    client, settings = dev_api
    conversation_id = await _new_conversation(client, settings)
    first = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                        {"text": "记一条：今天电话回访了客户"})
    second = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                         {"text": "再记一条：明天上门拜访"})
    assert first.json()["mode"] == "new_run"
    assert second.json()["mode"] == "new_run"
    assert first.json()["run_id"] != second.json()["run_id"]
    assert not second.json().get("idempotent_replay")

    messages = await _get(client, settings, f"/api/v1/conversations/{conversation_id}/messages")
    user_msgs = [m for m in messages.json()["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 2


async def test_same_client_key_replays_run_without_duplicate_message(dev_api):
    """同 key 重试：返回原 Run，消息不重复（M-07 弱网重复提交）。"""
    client, settings = dev_api
    conversation_id = await _new_conversation(client, settings)
    key = f"retry-{uuid.uuid4().hex}"
    first = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                        {"text": "记一条：客户A询价", "idempotency_key": key})
    replay = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                         {"text": "记一条：客户A询价", "idempotency_key": key})
    assert first.json()["run_id"] == replay.json()["run_id"]
    assert replay.json()["idempotent_replay"] is True

    messages = await _get(client, settings, f"/api/v1/conversations/{conversation_id}/messages")
    user_msgs = [m for m in messages.json()["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 1


async def test_resume_answer_not_duplicated_on_retry(dev_api):
    """补参应答重试：恢复已完成时重试同 key 幂等返回原 Run，应答消息不双插。"""
    client, settings = dev_api
    conversation_id = await _new_conversation(client, settings)
    key1 = f"m1-{uuid.uuid4().hex}"
    msg1 = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                       {"text": MISSING_TEXT, "idempotency_key": key1})
    run_id = msg1.json()["run_id"]
    await _wait_status(client, settings, run_id, {"waiting_input"})

    key2 = f"m2-{uuid.uuid4().hex}"
    answer = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                         {"text": "日期：2026-10-06", "idempotency_key": key2})
    assert answer.json()["mode"] == "resume"
    await _wait_status(client, settings, run_id, {"succeeded"})

    # 弱网重试：同 key 再发一次
    retry = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                        {"text": "日期：2026-10-06", "idempotency_key": key2})
    assert retry.status_code == 202

    messages = await _get(client, settings, f"/api/v1/conversations/{conversation_id}/messages")
    user_msgs = [m for m in messages.json()["messages"] if m["role"] == "user"]
    contents = [m["content"] for m in user_msgs]
    assert contents.count("日期：2026-10-06") == 1


# ---------------------------------------------------------------------------
# 3. 会话生命周期：关闭/过期在所有入口一致拒绝
# ---------------------------------------------------------------------------


def _qa_body(conversation_id: str) -> dict:
    return {
        "capability": "object.qa",
        "conversation_id": conversation_id,
        "input": {
            "question": "这个客户最近有什么进展？",
            "scope": {"subject_type": "customer", "subject_id": "C001"},
            "facts": [{"id": "fact-1", "type": "communication", "text": "客户确认下月签约"}],
        },
    }


async def test_closed_conversation_rejects_every_entry(dev_api):
    """closed 会话：post_message / create_run / waiting run 的 resume 全部拒绝。"""
    client, settings = dev_api
    conversation_id = await _new_conversation(client, settings)

    # 先造一个 waiting run，用于验证 close 后 resume 也被拒
    msg = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                      {"text": MISSING_TEXT, "idempotency_key": f"m-{uuid.uuid4().hex}"})
    run_id = msg.json()["run_id"]
    await _wait_status(client, settings, run_id, {"waiting_input"})

    closed = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/close", {})
    assert closed.status_code == 200

    reject_msg = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                             {"text": "还想再说一句"})
    assert reject_msg.status_code == 422

    reject_run = await _post(client, settings, "/api/v1/runs", _qa_body(conversation_id))
    assert reject_run.status_code == 422

    pending = await _get(client, settings, f"/api/v1/runs/{run_id}/pending")
    state_version = pending.json().get("state_version")
    reject_resume = await _post(client, settings, f"/api/v1/runs/{run_id}/resume",
                                {"values": {"due_date": "2026-10-06"}, "state_version": state_version})
    assert reject_resume.status_code == 422


async def test_ttl_expiry_marks_and_blocks(db_pool):
    """TTL 过期：只标记会话 expired 并拒绝续发；不影响会话内历史 Run/消息。"""
    from datetime import timedelta

    from app.util import utcnow

    conv = await conversations_repo.create_conversation(db_pool, crm_user_id="u1")
    conversation_id = conv["conversation_id"]
    await conversations_repo.append_message(
        db_pool, conversation_id=conversation_id, role="user", content="在吗"
    )
    run, _ = await runs_repo.create_run(
        db_pool,
        idempotency_key=f"ttl-{uuid.uuid4().hex}",
        capability="communication.extract",
        input_payload={"text": "记一条：客户A询价"},
        operator={"user_id": "u1"},
        thread_id=conv["thread_id"],
        conversation_id=conversation_id,
        graph_version="extract@1",
        prompt_version="extract-prompt@1",
        max_attempts=3,
    )

    # 把会话活动时间老化到 TTL 之外
    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE ai_conversations SET updated_at = %s WHERE conversation_id = %s",
            (utcnow() - timedelta(hours=100), conv["conversation_id"]),
        )
    expired = await conversations_repo.expire_stale_conversations(db_pool, ttl_hours=72)
    assert expired == 1

    refreshed = await conversations_repo.get_conversation(db_pool, conversation_id)
    assert refreshed["status"] == "expired"

    # 候选/业务状态不受影响：历史 Run 原样保留（M-01 红线）
    untouched = await runs_repo.get_run(db_pool, str(run["run_id"]))
    assert untouched is not None and untouched["status"] in {"queued", "running", "waiting_input", "succeeded", "failed"}


async def test_expired_conversation_rejected_at_api(dev_api):
    """expired 会话：续发消息 / 新建 Run 均拒绝（与 closed 一致）。"""
    client, settings = dev_api
    conversation_id = await _new_conversation(client, settings)
    async with open_pool(settings) as pool:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE ai_conversations SET status = 'expired' WHERE conversation_id = %s",
                (conversation_id,),
            )
    reject_msg = await _post(client, settings, f"/api/v1/conversations/{conversation_id}/messages",
                             {"text": "过期了还能发吗"})
    assert reject_msg.status_code == 422
    reject_run = await _post(client, settings, "/api/v1/runs", _qa_body(conversation_id))
    assert reject_run.status_code == 422


async def test_message_activity_refreshes_conversation_ttl(db_pool):
    """消息活动刷新会话 updated_at：持续对话的会话不会被 TTL 误杀。"""
    conv = await conversations_repo.create_conversation(db_pool, crm_user_id="u1")
    conversation_id = conv["conversation_id"]
    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE ai_conversations SET updated_at = now() - interval '71 hours' WHERE conversation_id = %s",
            (conv["conversation_id"],),
        )
    await conversations_repo.append_message(
        db_pool, conversation_id=conversation_id, role="user", content="刚说的一句话"
    )
    expired = await conversations_repo.expire_stale_conversations(db_pool, ttl_hours=72)
    assert expired == 0
    refreshed = await conversations_repo.get_conversation(db_pool, conversation_id)
    assert refreshed["status"] == "active"
