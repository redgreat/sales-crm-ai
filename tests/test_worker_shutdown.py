"""应用关闭时等待正常 worker，并限制卡住任务的退出时间。"""
from __future__ import annotations

import asyncio

import pytest

from app.runtime.executor import stop_workers


@pytest.mark.asyncio
async def test_stop_workers_allows_clean_exit() -> None:
    stop_event = asyncio.Event()

    async def worker() -> None:
        await stop_event.wait()

    task = asyncio.create_task(worker())
    await stop_workers([task], stop_event, timeout_seconds=0.1)
    assert task.done() and not task.cancelled()


@pytest.mark.asyncio
async def test_stop_workers_cancels_stuck_task() -> None:
    stop_event = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(worker())
    await asyncio.sleep(0)
    await stop_workers([task], stop_event, timeout_seconds=0.01)
    assert stop_event.is_set()
    assert cancelled.is_set()
    assert task.cancelled()
