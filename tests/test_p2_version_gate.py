"""旧图或 Prompt 版本的排队 Run 不得被当前代码误执行。"""
from __future__ import annotations

import uuid

import pytest

from app.capabilities import get_capability
from app.persistence import runs as runs_repo
from app.runtime.executor import Executor

pytestmark = pytest.mark.realpg


@pytest.mark.parametrize("outdated_field", ["graph_version", "prompt_version"])
async def test_outdated_run_version_fails_without_invoking_graph(db_pool, outdated_field: str):
    spec = get_capability("communication.extract")
    assert spec is not None
    versions = {"graph_version": spec.graph_version, "prompt_version": spec.prompt_version}
    versions[outdated_field] = "unsupported@0"
    run, _ = await runs_repo.create_run(
        db_pool,
        idempotency_key=f"p2-version-{uuid.uuid4().hex}",
        capability=spec.name,
        input_payload={"text": "客户：ACME\n任务：回访"},
        operator={"user_id": "p2-test"},
        thread_id=None,
        conversation_id=None,
        max_attempts=3,
        **versions,
    )

    class ForbiddenGraph:
        async def ainvoke(self, invoke_input, config):
            pytest.fail("旧版本 Run 不得调用当前图")

    executor = Executor(db_pool, {spec.name: ForbiddenGraph()}, lease_seconds=30)
    assert await executor.run_batch() == 1
    stored = await runs_repo.get_run(db_pool, run["run_id"])
    assert stored["status"] == "failed"
    assert stored["attempt_count"] == 1
    assert stored["result"] is None
    assert stored["error"]["code"] == "RUN_VERSION_UNSUPPORTED"
