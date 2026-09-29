"""Run 仓库不变量测试（真实 PostgreSQL）：幂等、并发认领、代次隔离、重试、取消、租约回收。"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.persistence import runs as runs_repo
from app.persistence.pool import open_pool
from app.util import utcnow

pytestmark = pytest.mark.realpg


def _make_run_kwargs(**overrides) -> dict:
    kwargs = dict(
        idempotency_key=f"test-{uuid.uuid4().hex}",
        capability="communication.extract",
        input_payload={"text": "客户：ACME\n任务：回访\n2026-10-01\n负责人：张三"},
        operator={"user_id": "u1"},
        thread_id=f"run-{uuid.uuid4().hex}",
        conversation_id=None,
        graph_version="extract@1",
        prompt_version="extract-prompt@1",
        max_attempts=3,
    )
    kwargs.update(overrides)
    return kwargs


async def test_create_run_idempotent(db_pool):
    kwargs = _make_run_kwargs()
    run1, created1 = await runs_repo.create_run(db_pool, **kwargs)
    run2, created2 = await runs_repo.create_run(db_pool, **kwargs)
    assert created1 and not created2
    assert run1["run_id"] == run2["run_id"]
    assert run1["status"] == "queued"


async def test_claim_is_exclusive_between_workers(db_pool):
    ids = []
    for _ in range(3):
        run, _ = await runs_repo.create_run(db_pool, **_make_run_kwargs())
        ids.append(run["run_id"])
    claimed_a = await runs_repo.claim_next_runs(db_pool, worker_id="w-a", limit=10, lease_seconds=60)
    claimed_b = await runs_repo.claim_next_runs(db_pool, worker_id="w-b", limit=10, lease_seconds=60)
    claimed_ids = [c["run_id"] for c in claimed_a + claimed_b]
    assert set(claimed_ids) == set(ids)
    assert len(claimed_ids) == len(set(claimed_ids))  # 无重复认领
    for claimed in claimed_a + claimed_b:
        assert claimed["status"] == "running"
        assert claimed["execution_generation"] == 1
        assert claimed["attempt_count"] == 1


async def test_stale_worker_cannot_complete(db_pool):
    """旧执行者迟到：租约过期被接管后，旧代次提交被拒绝。"""
    run, _ = await runs_repo.create_run(db_pool, **_make_run_kwargs())
    run_id = run["run_id"]
    first = (await runs_repo.claim_next_runs(db_pool, worker_id="w-1", limit=1, lease_seconds=60))[0]
    gen1 = first["execution_generation"]

    # 模拟 w-1 挂死：租约过期 → 回收 → 新 worker 认领（generation+1）
    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE ai_runs SET lease_expires_at = now() - interval '1s' WHERE run_id = %s",
            (run_id,),
        )
    requeued = await runs_repo.requeue_expired_leases(db_pool)
    assert requeued == [str(run_id)]
    second = (await runs_repo.claim_next_runs(db_pool, worker_id="w-2", limit=1, lease_seconds=60))[0]
    gen2 = second["execution_generation"]
    assert gen2 == gen1 + 1

    # 迟到的 w-1 提交被拒；w-2 提交成功
    ok1 = await runs_repo.complete_run(db_pool, run_id=run_id, generation=gen1, result={"x": "late"})
    assert ok1 is False
    ok2 = await runs_repo.complete_run(db_pool, run_id=run_id, generation=gen2, result={"x": "fresh"})
    assert ok2 is True
    final = await runs_repo.get_run(db_pool, run_id)
    assert final["status"] == "succeeded"
    assert final["result"] == {"x": "fresh"}


async def test_retry_then_exhaust(db_pool):
    run, _ = await runs_repo.create_run(db_pool, **_make_run_kwargs(max_attempts=2))
    run_id = run["run_id"]

    claimed = (await runs_repo.claim_next_runs(db_pool, worker_id="w", limit=1, lease_seconds=60))[0]
    outcome = await runs_repo.fail_run(
        db_pool, run_id=run_id, generation=claimed["execution_generation"],
        error={"code": "X", "message": "boom"}, backoff_seconds=0.1,
    )
    assert outcome == "queued"  # attempt 1/2 → 重试
    after = await runs_repo.get_run(db_pool, run_id)
    assert after["attempt_count"] == 1
    await asyncio.sleep(0.3)  # 等退避到期

    claimed2 = (await runs_repo.claim_next_runs(db_pool, worker_id="w", limit=1, lease_seconds=60))[0]
    outcome2 = await runs_repo.fail_run(
        db_pool, run_id=run_id, generation=claimed2["execution_generation"],
        error={"code": "X", "message": "boom again"}, backoff_seconds=0.1,
    )
    assert outcome2 == "failed"  # attempt 2/2 → 终态
    final = await runs_repo.get_run(db_pool, run_id)
    assert final["status"] == "failed"
    assert final["error"]["message"] == "boom again"


async def test_failed_run_backoff_delays_requeue(db_pool):
    run, _ = await runs_repo.create_run(db_pool, **_make_run_kwargs(max_attempts=3))
    claimed = (await runs_repo.claim_next_runs(db_pool, worker_id="w", limit=1, lease_seconds=60))[0]
    await runs_repo.fail_run(db_pool, run_id=run["run_id"], generation=claimed["execution_generation"],
                             error={"code": "X", "message": "boom"}, backoff_seconds=3600)
    claimed_again = await runs_repo.claim_next_runs(db_pool, worker_id="w", limit=10, lease_seconds=60)
    assert all(c["run_id"] != run["run_id"] for c in claimed_again)  # 退避期内不可再认领


async def test_cancel_poisons_in_flight_completion(db_pool):
    run, _ = await runs_repo.create_run(db_pool, **_make_run_kwargs())
    claimed = (await runs_repo.claim_next_runs(db_pool, worker_id="w", limit=1, lease_seconds=60))[0]
    cancelled = await runs_repo.cancel_run(db_pool, run_id=run["run_id"])
    assert cancelled
    ok = await runs_repo.complete_run(db_pool, run_id=run["run_id"],
                                      generation=claimed["execution_generation"], result={"late": True})
    assert ok is False
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "cancelled"
    assert final["result"] is None


async def test_waiting_and_resume_flow(db_pool):
    run, _ = await runs_repo.create_run(db_pool, **_make_run_kwargs())
    claimed = (await runs_repo.claim_next_runs(db_pool, worker_id="w", limit=1, lease_seconds=60))[0]
    assert await runs_repo.mark_waiting(db_pool, run_id=run["run_id"],
                                        generation=claimed["execution_generation"])
    waiting = await runs_repo.get_run(db_pool, run["run_id"])
    assert waiting["status"] == "waiting_input"
    assert waiting["lease_owner"] is None  # 等待不占租约

    # 重复/并发 resume：只有第一次 queue_resume 成功
    assert await runs_repo.queue_resume(db_pool, run_id=run["run_id"], values={"text": "日期：2026-10-01"})
    assert not await runs_repo.queue_resume(db_pool, run_id=run["run_id"], values={"text": "again"})
    resumed = await runs_repo.get_run(db_pool, run["run_id"])
    assert resumed["status"] == "queued"
    assert resumed["resume_values"] == {"text": "日期：2026-10-01"}
