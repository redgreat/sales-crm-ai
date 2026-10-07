"""跨真实 worker 进程边界验证 P2 恢复门槛。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.persistence import runs as runs_repo
from app.persistence.checkpoints import CheckpointSchemaNotReady, check_checkpoint_schema, open_postgres_saver
from app.runtime.executor import Executor

pytestmark = pytest.mark.realpg
WORKER = Path(__file__).parent / "support" / "p2_worker.py"


async def test_killed_worker_run_is_recovered_by_new_process(db_pool, db_url: str):
    run, created = await runs_repo.create_run(
        db_pool,
        idempotency_key=f"p2-crash-{uuid.uuid4().hex}",
        capability="communication.extract",
        input_payload={"text": "客户：ACME\n任务：回访\n2026-10-08\n负责人：张三"},
        operator={"user_id": "p2-test"},
        thread_id=None,
        conversation_id=None,
        graph_version="extract@1",
        prompt_version="extract-prompt@1",
        max_attempts=3,
    )
    assert created
    env = {**os.environ, "SAI_P2_TEST_DSN": db_url}
    first = subprocess.Popen(
        [sys.executable, str(WORKER), "claim"],
        cwd=WORKER.parents[2], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        claimed_line = await asyncio.wait_for(asyncio.to_thread(first.stdout.readline), timeout=15)
        assert claimed_line.strip() == "CLAIMED", first.stderr.read() if first.poll() is not None else ""
        running = await runs_repo.get_run(db_pool, run["run_id"])
        assert running["status"] == "running"
        assert running["execution_generation"] == 1
    finally:
        first.kill()
        await asyncio.to_thread(first.communicate, timeout=10)

    second = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(WORKER), "recover"],
        cwd=WORKER.parents[2], env=env, capture_output=True, text=True, timeout=20,
    )
    assert second.returncode == 0, second.stderr
    assert "RECOVERED" in second.stdout
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "succeeded"
    assert final["execution_generation"] == 2
    assert final["result_version"] == 1
    assert final["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-08"
    assert not await runs_repo.complete_run(
        db_pool, run_id=run["run_id"], generation=1, result={"stale": True}
    )


async def test_killed_during_model_call_recovers_without_duplicate_result(db_pool, db_url: str):
    run, _ = await runs_repo.create_run(
        db_pool,
        idempotency_key=f"p2-model-crash-{uuid.uuid4().hex}",
        capability="communication.extract",
        input_payload={"text": "客户：ACME\n任务：回访\n2026-10-08\n负责人：张三"},
        operator={"user_id": "p2-test"},
        thread_id=None,
        conversation_id=None,
        graph_version="extract@1",
        prompt_version="extract-prompt@1",
        max_attempts=3,
    )
    env = {**os.environ, "SAI_P2_TEST_DSN": db_url}
    first = subprocess.Popen(
        [sys.executable, str(WORKER), "execute_hold"],
        cwd=WORKER.parents[2], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        marker = await asyncio.wait_for(asyncio.to_thread(first.stdout.readline), timeout=15)
        assert marker.strip() == "MODEL_ENTERED", first.stderr.read() if first.poll() is not None else ""
        running = await runs_repo.get_run(db_pool, run["run_id"])
        assert running["status"] == "running"
        assert running["execution_generation"] == 1
    finally:
        first.kill()
        await asyncio.to_thread(first.communicate, timeout=10)

    second = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(WORKER), "recover"],
        cwd=WORKER.parents[2], env=env, capture_output=True, text=True, timeout=20,
    )
    assert second.returncode == 0, second.stderr
    final = await runs_repo.get_run(db_pool, run["run_id"])
    assert final["status"] == "succeeded"
    assert final["attempt_count"] == 2
    assert final["result_version"] == 1
    assert final["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-08"


async def test_checkpoint_version_mismatch_blocks_saver(db_pool, db_url: str):
    async with db_pool.connection() as conn:
        await conn.execute("UPDATE checkpoint_migrations SET v = 999 WHERE v = (SELECT max(v) FROM checkpoint_migrations)")
    ready, found = await check_checkpoint_schema(db_url)
    assert not ready
    assert found == 999
    with pytest.raises(CheckpointSchemaNotReady, match="版本不匹配"):
        async with open_postgres_saver(db_url):
            pytest.fail("Checkpoint saver opened with an unsupported schema version")


async def test_batch_does_not_lease_runs_before_they_can_execute(db_pool):
    for _ in range(3):
        await runs_repo.create_run(
            db_pool,
            idempotency_key=f"p2-batch-{uuid.uuid4().hex}",
            capability="communication.extract",
            input_payload={"text": "任务：回访"},
            operator={"user_id": "p2-test"},
            thread_id=None,
            conversation_id=None,
            graph_version="extract@1",
            prompt_version="extract-prompt@1",
            max_attempts=3,
        )

    started = asyncio.Event()
    release = asyncio.Event()

    class PausedGraph:
        async def ainvoke(self, invoke_input, config):
            started.set()
            await release.wait()
            return {"result": {"candidates": {"tasks": []}}}

        async def aget_state(self, config):
            return type("State", (), {"next": ()})()

    executor = Executor(db_pool, {"communication.extract": PausedGraph()}, lease_seconds=1, batch_size=3)
    batch = asyncio.create_task(executor.run_batch())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with db_pool.connection() as conn:
            cursor = await conn.execute("SELECT status, count(*) FROM ai_runs GROUP BY status")
            statuses = dict(await cursor.fetchall())
        assert statuses == {"running": 1, "queued": 2}
    finally:
        release.set()
        await asyncio.wait_for(batch, timeout=10)
    assert batch.result() == 3


async def test_queued_backlog_drains_once_with_two_workers(db_pool):
    """本地积压任务由两个 worker 消费，每个 Run 只完成一次。"""
    run_ids = set()
    for _ in range(40):
        run, _ = await runs_repo.create_run(
            db_pool,
            idempotency_key=f"p2-backlog-{uuid.uuid4().hex}",
            capability="communication.extract",
            input_payload={"text": "任务：回访"},
            operator={"user_id": "p2-backlog"},
            thread_id=None,
            conversation_id=None,
            graph_version="extract@1",
            prompt_version="extract-prompt@1",
            max_attempts=3,
        )
        run_ids.add(run["run_id"])

    class TinyGraph:
        async def ainvoke(self, invoke_input, config):
            await asyncio.sleep(0.02)
            return {"result": {"candidates": {"tasks": []}}}

        async def aget_state(self, config):
            return type("State", (), {"next": ()})()

    async def drain(executor: Executor) -> None:
        while await executor.run_batch():
            pass

    graph = TinyGraph()
    await asyncio.gather(*(
        drain(Executor(db_pool, {"communication.extract": graph}, lease_seconds=1, batch_size=1))
        for _ in range(2)
    ))
    rows = await runs_repo.list_runs(db_pool, operator_user_id="p2-backlog", limit=50)
    assert {row["run_id"] for row in rows} == run_ids
    assert all(row["status"] == "succeeded" for row in rows)
    assert all(row["result_version"] == 1 and row["attempt_count"] == 1 for row in rows)
