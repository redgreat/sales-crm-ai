"""执行器测试（真实 PostgreSQL）：等待/恢复/崩溃恢复迟到隔离/重试。"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.persistence import conversations as conv_repo
from app.persistence import runs as runs_repo
from app.runtime.executor import Executor
from app.util import utcnow

pytestmark = pytest.mark.realpg


def _input_kwargs(**overrides) -> dict:
    kwargs = dict(
        idempotency_key=f"exec-{uuid.uuid4().hex}",
        capability="communication.extract",
        input_payload={"text": "客户：ACME\n任务：回访\n2026-10-01\n负责人：张三"},
        operator={"user_id": "u1"},
        thread_id=None,
        conversation_id=None,
        graph_version="extract@1",
        prompt_version="extract-prompt@1",
        max_attempts=3,
    )
    kwargs.update(overrides)
    return kwargs


async def test_executor_runs_to_success(db_pool, graphs):
    executor = Executor(db_pool, graphs, lease_seconds=60, backoff_base_seconds=0.1)
    run, _ = await runs_repo.create_run(db_pool, **_input_kwargs())
    processed = await executor.run_batch()
    assert processed == 1
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "succeeded"
    assert final["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-01"
    assert final["result_version"] == 1


async def test_executor_waits_then_resumes(db_pool, graphs):
    executor = Executor(db_pool, graphs, lease_seconds=60, backoff_base_seconds=0.1)
    run, _ = await runs_repo.create_run(
        db_pool, **_input_kwargs(input_payload={"text": "[[缺日期]]\n任务：跟进\n客户：ACME\n负责人：张三"})
    )
    await executor.run_batch()
    waiting = await runs_repo.get_run(db_pool, run["run_id"])
    assert waiting["status"] == "waiting_input"
    assert waiting["lease_owner"] is None

    await executor.run_batch()  # 没有排队任务：等待中的 Run 不应被再次执行
    assert (await runs_repo.get_run(db_pool, run["run_id"]))["status"] == "waiting_input"

    assert await runs_repo.queue_resume(db_pool, run_id=run["run_id"], values={"text": "日期：2026-10-08"})
    await executor.run_batch()
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "succeeded"
    assert final["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-08"


async def test_executor_unknown_capability_fails_run(db_pool, graphs):
    executor = Executor(db_pool, graphs, lease_seconds=60, backoff_base_seconds=0.1)
    run, _ = await runs_repo.create_run(db_pool, **_input_kwargs(capability="nope.extract", max_attempts=1))
    await executor.run_batch()
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "failed"
    assert final["error"]["code"] == "CAPABILITY_UNKNOWN"


async def test_crash_recovery_with_lease_takeover(db_pool, graphs):
    """worker 崩溃：租约过期回收后由新 worker 完成；旧 worker 迟到提交被代次守卫拒绝。"""
    executor = Executor(db_pool, graphs, lease_seconds=60, backoff_base_seconds=0.1)
    run, _ = await runs_repo.create_run(db_pool, **_input_kwargs())

    # 第一个 worker 认领后"崩溃"（不执行、租约过期）
    claimed = (await runs_repo.claim_next_runs(db_pool, worker_id="dead-worker", limit=1, lease_seconds=1))[0]
    await asyncio.sleep(1.1)
    requeued = await runs_repo.requeue_expired_leases(db_pool)
    assert requeued == [str(run["run_id"])]

    # 新执行器接管并完成
    processed = await executor.run_batch()
    assert processed == 1
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "succeeded"

    # 崩溃 worker 复活迟到提交：代次已变，拒绝
    ok = await runs_repo.complete_run(
        db_pool, run_id=run["run_id"], generation=claimed["execution_generation"],
        result={"candidates": {"stale": True}},
    )
    assert ok is False
    assert (await runs_repo.get_run(db_pool, run["run_id"]))["result"] == final["result"]


async def test_crash_during_resume_recovers_from_checkpoint(db_pool, graphs):
    """恢复执行中途崩溃：checkpoint 已消费 interrupt 但结果未落库时，
    重新调度后从 checkpoint 状态直接取结果落库，不重复执行图。"""
    executor = Executor(db_pool, graphs, lease_seconds=60, backoff_base_seconds=0.1)
    run, _ = await runs_repo.create_run(
        db_pool, **_input_kwargs(input_payload={"text": "[[缺日期]]\n任务：跟进\n客户：ACME\n负责人：张三"})
    )
    await executor.run_batch()
    assert (await runs_repo.get_run(db_pool, run["run_id"]))["status"] == "waiting_input"

    await runs_repo.queue_resume(db_pool, run_id=run["run_id"], values={"text": "日期：2026-10-09"})
    # 模拟 resume 执行后、complete 前崩溃：手动推进图（消费 interrupt → 图完成），
    # 然后"崩溃 worker"的租约过期，重新调度
    claimed = (await runs_repo.claim_next_runs(db_pool, worker_id="dead-worker", limit=1, lease_seconds=1))[0]
    from langgraph.types import Command

    graph = graphs["communication.extract"]
    final_state = await graph.ainvoke(Command(resume={"text": "日期：2026-10-09"}),
                                      config={"configurable": {"thread_id": claimed["thread_id"]}})
    assert final_state["result"] is not None
    await asyncio.sleep(1.1)
    await executor.reap_expired_leases()

    processed = await executor.run_batch()  # 新执行器接管 resume
    assert processed == 1
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "succeeded"
    assert final["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-09"


async def test_conversation_run_appends_assistant_message(db_pool, graphs):
    executor = Executor(db_pool, graphs, lease_seconds=60, backoff_base_seconds=0.1)
    conversation = await conv_repo.create_conversation(db_pool, crm_user_id="u1")
    run, _ = await runs_repo.create_run(
        db_pool,
        **_input_kwargs(
            thread_id=conversation["thread_id"],
            conversation_id=conversation["conversation_id"],
        ),
    )
    await executor.run_batch()
    messages = await conv_repo.list_messages(db_pool, conversation["conversation_id"])
    assistant = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert "候选建议" in assistant[0]["content"]
    # 落库内容不含 usage/token 元数据
    import json

    meta_text = json.dumps(assistant[0]["meta"], ensure_ascii=False).lower()
    assert "usage" not in meta_text and "token" not in meta_text


async def test_long_running_graph_renews_lease(db_pool, monkeypatch):
    """图执行超过单次租约时应续租，避免被另一 worker 回收并重复执行。"""
    renewals = 0
    original_renew = runs_repo.renew_lease

    async def counted_renew(*args, **kwargs):
        nonlocal renewals
        renewals += 1
        return await original_renew(*args, **kwargs)

    monkeypatch.setattr(runs_repo, "renew_lease", counted_renew)
    run, _ = await runs_repo.create_run(db_pool, **_input_kwargs())
    claimed = (
        await runs_repo.claim_next_runs(
            db_pool, worker_id="slow-worker", limit=1, lease_seconds=1
        )
    )[0]

    class SlowGraph:
        async def ainvoke(self, invoke_input, config):
            await asyncio.sleep(1.2)
            return {"result": {"candidates": {"activities": [], "tasks": []}}}

        async def aget_state(self, config):
            return type("State", (), {"next": ()})()

    executor = Executor(
        db_pool,
        {"communication.extract": SlowGraph()},
        lease_seconds=1,
        backoff_base_seconds=0.1,
    )
    await executor._execute_claimed(claimed)
    assert renewals >= 2
    assert (await runs_repo.get_run(db_pool, run["run_id"]))["status"] == "succeeded"
