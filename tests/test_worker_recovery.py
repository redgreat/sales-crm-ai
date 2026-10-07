"""worker 扫描遇到短暂故障后保持运行并继续处理。"""
from __future__ import annotations

import asyncio

import psycopg
import psycopg_pool
import pytest

from app.runtime.executor import run_worker_loop


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [psycopg.OperationalError, psycopg_pool.PoolTimeout])
async def test_worker_retries_after_transient_failure(failure: type[Exception]) -> None:
    stop_event = asyncio.Event()

    class TransientExecutor:
        calls = 0

        async def run_batch(self) -> int:
            self.calls += 1
            if self.calls == 1:
                raise failure("temporary database outage")
            stop_event.set()
            return 1

        async def reap_expired_leases(self) -> int:
            return 0

        async def expire_stale_conversations(self) -> int:
            return 0

    executor = TransientExecutor()
    await asyncio.wait_for(
        run_worker_loop(
            executor,
            poll_interval_seconds=0.01,
            reap_interval_seconds=0.01,
            stop_event=stop_event,
        ),
        timeout=2.0,
    )
    assert executor.calls == 2


@pytest.mark.asyncio
async def test_worker_exits_on_programming_error() -> None:
    class BrokenExecutor:
        async def run_batch(self) -> int:
            raise ValueError("invalid run state")

    with pytest.raises(ValueError, match="invalid run state"):
        await run_worker_loop(
            BrokenExecutor(),
            poll_interval_seconds=0.01,
            reap_interval_seconds=0.01,
            stop_event=asyncio.Event(),
        )
