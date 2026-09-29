"""运行时执行器：认领队列任务、执行图、持久化结果。

- checkpoint 负责图状态与中断恢复；本执行器负责认领租约、代次、重试与取消；
- 等待（interrupt）不占 worker：标记 waiting_input 后释放；
- 恢复执行（resume_values 存在）用 Command(resume=...) 续跑同一线程；
- 外部调用（模型）不持数据库事务；每步守卫 execution_generation。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import psycopg_pool
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.persistence import runs as runs_repo
from app.util import utcnow

logger = logging.getLogger("sales-crm-ai.executor")


class RunFailure(Exception):
    def __init__(self, message: str, *, code: str = "GRAPH_FAILED"):
        super().__init__(message)
        self.code = code


def _has_pending_interrupt(graph: CompiledStateGraph, thread_id: str) -> bool:
    state = graph.get_state({"configurable": {"thread_id": thread_id}})
    return bool(state.next)


async def _has_pending_interrupt_async(graph: CompiledStateGraph, thread_id: str) -> bool:
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return bool(state.next)


class Executor:
    def __init__(
        self,
        pool: psycopg_pool.AsyncConnectionPool,
        graphs: dict[str, CompiledStateGraph],
        *,
        lease_seconds: int,
        max_attempts_default: int = 3,
        backoff_base_seconds: float = 2.0,
        batch_size: int = 4,
    ):
        self.pool = pool
        self.graphs = graphs
        self.lease_seconds = lease_seconds
        self.max_attempts_default = max_attempts_default
        self.backoff_base_seconds = backoff_base_seconds
        self.batch_size = batch_size

    @property
    def worker_id(self) -> str:
        return f"worker-{uuid.uuid4().hex[:12]}"

    async def run_batch(self) -> int:
        """认领并执行一批；返回处理数量。单批内串行执行（并发由多 worker 提供）。"""
        claimed = await runs_repo.claim_next_runs(
            self.pool,
            worker_id=self.worker_id,
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        for run in claimed:
            await self._execute_claimed(run)
        return len(claimed)

    async def _execute_claimed(self, run: dict[str, Any]) -> None:
        run_id = str(run["run_id"])
        generation = int(run["execution_generation"])
        capability = run["capability"]
        graph = self.graphs.get(capability)
        try:
            if graph is None:
                raise RunFailure(f"能力未注册: {capability}", code="CAPABILITY_UNKNOWN")

            config = {"configurable": {"thread_id": run["thread_id"]}}
            resume_values = run.get("resume_values")

            if resume_values is not None:
                if not await _has_pending_interrupt_async(graph, run["thread_id"]):
                    # 崩溃恢复：resume 已被消费但结果未落库 → 直接从 checkpoint 状态取结果
                    state = await graph.aget_state(config)
                    stored = (state.values or {}).get("result")
                    if stored is not None:
                        await runs_repo.complete_run(
                            self.pool, run_id=run_id, generation=generation, result=stored
                        )
                        return
                    await runs_repo.fail_run(
                        self.pool,
                        run_id=run_id,
                        generation=generation,
                        error={"code": "RESUME_STATE_LOST", "message": "checkpoint 无等待状态也无结果"},
                        backoff_seconds=self.backoff_base_seconds,
                    )
                    return
                invoke_input: Any = Command(resume=resume_values)
            else:
                run_input = run.get("input") or {}
                invoke_input = {
                    "user_text": str(run_input.get("text", "")),
                    "capability": capability,
                    "input_payload": run_input,
                }

            started = time.monotonic()
            try:
                final_state = await graph.ainvoke(invoke_input, config=config)
            except Exception as exc:  # noqa: BLE001 —— 执行失败统一转 run 失败
                raise RunFailure(str(exc), code="GRAPH_FAILED") from exc
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if error := final_state.get("error"):
                await self._handle_failure(run_id, generation, {"code": "GRAPH_ERROR", "message": str(error)})
                return

            if "__interrupt__" in final_state or await _has_pending_interrupt_async(
                graph, run["thread_id"]
            ):
                # 进入等待：释放 worker，不占租约
                await runs_repo.mark_waiting(self.pool, run_id=run_id, generation=generation)
                return

            result = final_state.get("result")
            if result is None:
                await self._handle_failure(
                    run_id, generation,
                    {"code": "EMPTY_RESULT", "message": "图结束但没有产出结果"},
                )
                return

            persisted = await runs_repo.complete_run(
                self.pool, run_id=run_id, generation=generation, result=result
            )
            if persisted:
                logger.info("run %s succeeded in %dms (gen %d)", run_id, elapsed_ms, generation)
                await self._append_assistant_message(run, result)
            else:
                # 代次不匹配：已被接管或取消，迟到结果丢弃（不覆盖）
                logger.warning("run %s completion rejected (stale generation %d)", run_id, generation)
        except RunFailure as exc:
            await self._handle_failure(run_id, generation, {"code": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("run %s executor crash", run_id)
            await self._handle_failure(run_id, generation, {"code": "EXECUTOR_CRASH", "message": str(exc)})

    async def _append_assistant_message(self, run: dict[str, Any], result: dict[str, Any]) -> None:
        """会话内运行的助手消息落库（脱敏文本，不含 usage/token 元数据）。"""
        conversation_id = run.get("conversation_id")
        if not conversation_id:
            return
        candidates = (result or {}).get("candidates") or {}
        tasks = candidates.get("tasks") or []
        summary = "；".join(t.get("title", "") for t in tasks) or "未产生任务候选"
        from app.persistence import conversations as conversations_repo

        await conversations_repo.append_message(
            self.pool,
            conversation_id=str(conversation_id),
            role="assistant",
            content=f"已生成候选建议：{summary}（待本人确认）",
            run_id=str(run["run_id"]),
            meta={"candidates": result},
        )

    async def _handle_failure(self, run_id: str, generation: int, error: dict[str, Any]) -> None:
        outcome = await runs_repo.fail_run(
            self.pool,
            run_id=run_id,
            generation=generation,
            error=error,
            backoff_seconds=self.backoff_base_seconds,
        )
        if outcome is None:
            logger.warning("run %s failure write rejected (stale generation %d)", run_id, generation)

    async def reap_expired_leases(self) -> int:
        requeued = await runs_repo.requeue_expired_leases(self.pool)
        if requeued:
            logger.info("requeued %d runs with expired leases", len(requeued))
        return len(requeued)


async def run_worker_loop(
    executor: Executor,
    *,
    poll_interval_seconds: float,
    reap_interval_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    """worker 主循环：扫描队列 + 周期回收过期租约。"""
    last_reap = 0.0
    while not stop_event.is_set():
        processed = await executor.run_batch()
        now = time.monotonic()
        if now - last_reap >= reap_interval_seconds:
            await executor.reap_expired_leases()
            last_reap = now
        if not processed:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                pass
