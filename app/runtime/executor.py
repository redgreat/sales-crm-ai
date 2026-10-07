"""运行时执行器：认领队列任务、执行图、持久化结果。

- checkpoint 负责图状态与中断恢复；本执行器负责认领租约、代次、重试与取消；
- 等待（interrupt）不占 worker：标记 waiting_input 后释放；
- 恢复执行（resume_values 存在）用 Command(resume=...) 续跑同一线程；
- 外部调用（模型）不持数据库事务；每步守卫 execution_generation。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import Any

import psycopg
import psycopg_pool
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.capabilities import get_capability
from app.auth import OperatorContext
from app.enhanced_input import StageError, ensure_recognition
from app.integrations.organize import organize_text
from app.knowledge import (
    doc_as_fact,
    filter_unauthorized_history,
    search_authorized,
)
from app.persistence import conversations as conversations_repo
from app.persistence import runs as runs_repo
from app.util import utcnow

logger = logging.getLogger("sales-crm-ai.executor")

# 问答族单次注入的知识条数上限（授权过滤在 SQL 层，先于内容进入模型）
QA_KNOWLEDGE_LIMIT = 5


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
        conversations_ttl_hours: int = 72,
        settings: Any | None = None,
        crm_client: Any | None = None,
        model: Any | None = None,
    ):
        self.pool = pool
        self.graphs = graphs
        self.lease_seconds = lease_seconds
        self.max_attempts_default = max_attempts_default
        self.backoff_base_seconds = backoff_base_seconds
        self.batch_size = batch_size
        # <=0 表示禁用会话 TTL 过期清理
        self.conversations_ttl_hours = conversations_ttl_hours
        self.settings = settings
        self.crm_client = crm_client
        self.model = model

    async def _prepare_file_input(self, run: dict[str, Any], run_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if self.settings is None or self.crm_client is None or self.model is None:
            raise RunFailure("file_ref 执行环境缺少 CRM 客户端或模型", code="FILE_INPUT_UNAVAILABLE")
        operator = run.get("operator") or {}
        try:
            recognized = await ensure_recognition(
                self.pool, crm_client=self.crm_client,
                operator=OperatorContext(user_id=str(operator.get("user_id") or ""),
                                         user_name=str(operator.get("user_name") or "")),
                file_ref=run_input["file_ref"], settings=self.settings,
                run_id=str(run["run_id"]),
            )
            row = recognized["row"]
            optimized = await organize_text(self.model, row["raw_text"])
        except StageError as exc:
            raise RunFailure(exc.message, code=f"FILE_{exc.stage.upper()}_{exc.code}") from exc
        layers = {
            "file_id": row["file_id"], "source_version": row["source_version"],
            "processing": row["processing"], "recognition_id": row["recognition_id"],
            "raw_text": row["raw_text"], "raw_version": row["recognition_id"],
            "anchored_text": row["anchored_text"], "evidence": row["evidence"],
            "optimized_text": optimized,
            "optimized_version": "sha256:" + hashlib.sha256(
                (row["recognition_id"] + "|" + optimized).encode("utf-8")
            ).hexdigest()[:16],
            "final_text": None, "final_version": None,
            "requires_human_confirmation": True,
        }
        return optimized, layers

    @property
    def worker_id(self) -> str:
        return f"worker-{uuid.uuid4().hex[:12]}"

    async def run_batch(self) -> int:
        """串行处理一批；每次仅认领即将执行的 Run，避免排队时租约过期。"""
        processed = 0
        for _ in range(self.batch_size):
            claimed = await runs_repo.claim_next_runs(
                self.pool,
                worker_id=self.worker_id,
                limit=1,
                lease_seconds=self.lease_seconds,
            )
            if not claimed:
                break
            await self._execute_claimed(claimed[0])
            processed += 1
        return processed

    async def _qa_context(self, spec: Any, run: dict[str, Any]) -> dict[str, Any]:
        """问答族执行前上下文装配（需求 4.6～4.9）。

        - 知识：按操作者授权检索（过滤在 SQL 层，先于内容进入模型），
          只把有权且已发布的最新版本注入图，不先取无权全文再隐藏引用；
        - 历史：对象内追问使用会话有效历史，仅作上下文（`[H]` 行），
          不进引用白名单（历史不是事实来源）；且**引用过已撤权知识的旧回答
          会被剔除**（M-10）——否则撤权内容会借历史绕回模型。
        """
        context: dict[str, Any] = {}
        user_id = str((run.get("operator") or {}).get("user_id") or "").strip()
        needs_knowledge = bool(spec.retrieves_knowledge and user_id)
        needs_history = bool(spec.requires_history and run.get("conversation_id"))
        if not (needs_knowledge or needs_history):
            return context

        # 知识与历史共用一条连接：少一次连接池借还，也避免两次往返
        async with self.pool.connection() as conn:
            if spec.retrieves_knowledge and not user_id:
                logger.warning("run %s 缺少 operator.user_id，跳过知识检索", run.get("run_id"))
            if needs_knowledge:
                # 带上问题做字面相关度重排：否则注入的是"最近几条"而非"相关的几条"
                question = str((run.get("input") or {}).get("question") or "").strip()
                if not question:
                    topic = (run.get("input") or {}).get("meeting")
                    if isinstance(topic, dict):
                        question = str(topic.get("topic") or "").strip()
                docs = await search_authorized(
                    conn, user_id=user_id, limit=QA_KNOWLEDGE_LIMIT, query=question or None
                )
                context["knowledge_facts"] = [doc_as_fact(doc) for doc in docs]

            if needs_history:
                messages = await conversations_repo.list_messages(
                    conn, str(run["conversation_id"])
                )
                history = [
                    {"role": m.get("role"), "content": m.get("content"), "meta": m.get("meta")}
                    for m in messages
                    if m.get("content")
                ]
                # 撤权拦截（M-10）：引用了已撤权/已无权知识的旧回答不得作为上下文入模。
                if user_id:
                    history, dropped = await filter_unauthorized_history(
                        conn, user_id=user_id, history=history
                    )
                    if dropped:
                        logger.info(
                            "run %s 历史中剔除 %d 条引用已撤权知识的旧回答",
                            run.get("run_id"),
                            len(dropped),
                        )
                context["history"] = history
        return context

    async def _execute_claimed(self, run: dict[str, Any]) -> None:
        run_id = str(run["run_id"])
        generation = int(run["execution_generation"])
        capability = run["capability"]
        graph = self.graphs.get(capability)
        heartbeat = asyncio.create_task(
            self._renew_lease_until_done(run_id, generation),
            name=f"lease-heartbeat:{run_id}",
        )
        try:
            if graph is None:
                raise RunFailure(f"能力未注册: {capability}", code="CAPABILITY_UNKNOWN")

            spec = get_capability(capability)
            if spec is not None and (
                run["graph_version"] != spec.graph_version
                or run["prompt_version"] != spec.prompt_version
            ):
                await runs_repo.fail_run(
                    self.pool,
                    run_id=run_id,
                    generation=generation,
                    error={"code": "RUN_VERSION_UNSUPPORTED", "message": "Run 版本与当前能力不兼容"},
                    backoff_seconds=0,
                    terminal=True,
                )
                logger.warning("run %s rejected: unsupported graph or prompt version", run_id)
                return

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
                file_layers = None
                user_text = str(run_input.get("text", ""))
                if "file_ref" in run_input:
                    user_text, file_layers = await self._prepare_file_input(run, run_input)
                    run_input = {**run_input, "_enhanced_input": file_layers}
                invoke_input = {
                    "user_text": user_text,
                    "capability": capability,
                    "input_payload": run_input,
                }
                spec = get_capability(capability)
                if spec is not None and (spec.retrieves_knowledge or spec.requires_history):
                    invoke_input.update(await self._qa_context(spec, run))

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

            layers = (final_state.get("input_payload") or {}).get("_enhanced_input")
            if layers:
                result = {**result, "enhanced_input": layers}

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
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _renew_lease_until_done(self, run_id: str, generation: int) -> None:
        """执行期间周期续租；状态或代次变化后自然停止。"""
        interval = max(0.1, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await runs_repo.renew_lease(
                    self.pool,
                    run_id=run_id,
                    generation=generation,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:  # noqa: BLE001 —— 短暂数据库故障由后续心跳继续尝试
                logger.exception("run %s lease renewal failed (gen %d)", run_id, generation)
                continue
            if not renewed:
                return

    async def _append_assistant_message(self, run: dict[str, Any], result: dict[str, Any]) -> None:
        """会话内运行的助手消息落库（脱敏文本，不含 usage/token 元数据）。

        抽取型记录候选建议（待本人确认）；只读问答/分析型记录回答本身，
        不套用候选模板——避免把只读结论误写成"待确认候选"。
        """
        conversation_id = run.get("conversation_id")
        if not conversation_id:
            return
        candidates = (result or {}).get("candidates") or {}
        tasks = candidates.get("tasks") or []
        if tasks:
            summary = "；".join(str(t.get("title", "")) for t in tasks)
            content = f"已生成候选建议：{summary}（待本人确认）"
        else:
            content = str((result or {}).get("summary") or "").strip() or "（无可用依据，未生成结论）"

        # 记录本条回答引用过的知识 id：撤权后据此把该条从历史中剔除（M-10），
        # 否则被撤权的内容会借"会话历史"再次进入模型。
        knowledge_used = (result or {}).get("knowledge_used") or {}
        cited = [
            str(ref.get("ref_id")).strip()
            for ref in (knowledge_used.get("refs") or [])
            if isinstance(ref, dict) and str(ref.get("ref_id") or "").strip()
        ]
        meta: dict[str, Any] = {"candidates": result}
        if cited:
            meta["knowledge_refs"] = cited

        await conversations_repo.append_message(
            self.pool,
            conversation_id=str(conversation_id),
            role="assistant",
            content=content,
            run_id=str(run["run_id"]),
            meta=meta,
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

    async def expire_stale_conversations(self) -> int:
        """会话 TTL 过期清理（P3）：只改会话状态，不碰任何候选/Run 业务状态。"""
        if self.conversations_ttl_hours <= 0:
            return 0
        expired = await conversations_repo.expire_stale_conversations(
            self.pool, ttl_hours=self.conversations_ttl_hours
        )
        if expired:
            logger.info("expired %d stale conversations (ttl=%dh)", expired, self.conversations_ttl_hours)
        return expired


async def run_worker_loop(
    executor: Executor,
    *,
    poll_interval_seconds: float,
    reap_interval_seconds: float,
    stop_event: asyncio.Event,
    conversation_sweep_interval_seconds: float = 600.0,
) -> None:
    """worker 主循环：扫描队列 + 周期回收过期租约 + 低频会话 TTL 清理。"""
    last_reap = 0.0
    last_sweep = 0.0
    while not stop_event.is_set():
        try:
            processed = await executor.run_batch()
            now = time.monotonic()
            if now - last_reap >= reap_interval_seconds:
                await executor.reap_expired_leases()
                last_reap = now
            if now - last_sweep >= conversation_sweep_interval_seconds:
                try:
                    await executor.expire_stale_conversations()
                except Exception:  # noqa: BLE001 —— 清理失败不影响队列主流程
                    logger.exception("conversation ttl sweep failed")
                last_sweep = now
        except (psycopg.Error, psycopg_pool.PoolTimeout, TimeoutError):
            # 数据库短暂失联后继续扫描；其他程序错误交给 /ready 报警。
            logger.exception("worker iteration failed; retrying")
            delay = max(1.0, min(reap_interval_seconds, 5.0))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            continue
        if not processed:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                pass


async def stop_workers(
    workers: list[asyncio.Task[Any]], stop_event: asyncio.Event, *, timeout_seconds: float
) -> None:
    """先请求正常退出，超过宽限期后取消仍在执行的 worker。"""
    stop_event.set()
    if not workers:
        return
    done, pending = await asyncio.wait(workers, timeout=timeout_seconds)
    if pending:
        logger.warning("%d worker did not stop within %.1fs; cancelling", len(pending), timeout_seconds)
        for task in pending:
            task.cancel()
        cancelled, pending = await asyncio.wait(pending, timeout=1.0)
        done.update(cancelled)
    if pending:
        logger.error("%d worker did not respond to cancellation", len(pending))
    for task in done:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("worker exited with error", exc_info=(type(error), error, error.__traceback__))
