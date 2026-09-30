"""Run 仓库：队列、幂等、租约与执行代次。

关键不变量（需求 5.1）：
- Run 与幂等键原子创建，重复提交返回原 Run；
- 认领用 FOR UPDATE SKIP LOCKED，两 worker 不会拿到同一 Run；
- 完成/失败带 execution_generation 守卫：租约过期被接管后，旧执行者迟到提交一律拒绝；
- cancel 递增 generation，毒化任何在途提交；
- 所有语句都是短事务，模型调用不持有数据库事务。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.util import new_uuid, utcnow


def canonical_input_hash(input_payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _history_entry(status: str, **extra: Any) -> str:
    entry = {"status": status, "at": utcnow().isoformat(), **extra}
    return json.dumps([entry], ensure_ascii=False, separators=(",", ":"))


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("run_id", "conversation_id"):
        if isinstance(out.get(key), UUID):
            out[key] = str(out[key])
    return out


async def create_run(
    pool: psycopg.AsyncConnectionPool,
    *,
    idempotency_key: str,
    capability: str,
    input_payload: dict[str, Any],
    operator: dict[str, Any],
    thread_id: str | None,
    conversation_id: str | None,
    graph_version: str,
    prompt_version: str,
    max_attempts: int,
) -> tuple[dict[str, Any], bool]:
    """创建 Run；幂等键冲突时返回已有 Run（created=False）。

    thread_id 缺省为 run 级独立线程 run:{run_id}；会话内则传会话线程。
    """
    run_id = new_uuid()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO ai_runs (run_id, idempotency_key, capability, status, input,
                    input_hash, thread_id, conversation_id, operator, graph_version,
                    prompt_version, max_attempts)
                VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    run_id,
                    idempotency_key,
                    capability,
                    json.dumps(input_payload, ensure_ascii=False),
                    canonical_input_hash(input_payload),
                    thread_id or f"run:{run_id}",
                    UUID(conversation_id) if conversation_id else None,
                    json.dumps(operator, ensure_ascii=False),
                    graph_version,
                    prompt_version,
                    max_attempts,
                ),
            )
            row = await cur.fetchone()
            if row is not None:
                return _row_to_dict(row), True
            await cur.execute("SELECT * FROM ai_runs WHERE idempotency_key = %s", (idempotency_key,))
            existing = await cur.fetchone()
            if existing is None:
                raise RuntimeError("Run 创建冲突：幂等键存在但查询不到")
            return _row_to_dict(existing), False


async def get_run(pool: psycopg.AsyncConnectionPool, run_id: str) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM ai_runs WHERE run_id = %s", (UUID(run_id),))
            row = await cur.fetchone()
            return _row_to_dict(row) if row else None


async def find_latest_run_by_status(
    pool: psycopg.AsyncConnectionPool, *, conversation_id: str, status: str
) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM ai_runs
                WHERE conversation_id = %s AND status = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (UUID(conversation_id), status),
            )
            row = await cur.fetchone()
            return _row_to_dict(row) if row else None


async def claim_next_runs(
    pool: psycopg.AsyncConnectionPool,
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int,
) -> list[dict[str, Any]]:
    """认领到期任务：generation+1、attempt_count+1、写入租约。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH picked AS (
                        SELECT run_id FROM ai_runs
                        WHERE status = 'queued' AND run_after <= now()
                        ORDER BY run_after
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE ai_runs r
                    SET status = 'running',
                        lease_owner = %s,
                        lease_expires_at = now() + make_interval(secs => %s),
                        execution_generation = r.execution_generation + 1,
                        attempt_count = r.attempt_count + 1,
                        started_at = COALESCE(r.started_at, now()),
                        updated_at = now(),
                        status_history = r.status_history || %s::jsonb
                    FROM picked
                    WHERE r.run_id = picked.run_id
                    RETURNING r.*
                    """,
                    (
                        limit,
                        worker_id,
                        float(lease_seconds),
                        _history_entry("running", owner=worker_id),
                    ),
                )
                rows = await cur.fetchall()
                return [_row_to_dict(row) for row in rows]


async def renew_lease(
    pool: psycopg.AsyncConnectionPool, *, run_id: str, generation: int, lease_seconds: int
) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai_runs SET lease_expires_at = now() + make_interval(secs => %s)
                WHERE run_id = %s AND status = 'running' AND execution_generation = %s
                """,
                (float(lease_seconds), UUID(run_id), generation),
            )
            return cur.rowcount == 1


async def complete_run(
    pool: psycopg.AsyncConnectionPool,
    *,
    run_id: str,
    generation: int,
    result: dict[str, Any],
) -> bool:
    """持久化成功结果；代次不匹配（被接管/已取消）时拒绝并返回 False。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai_runs
                SET status = 'succeeded',
                    result = %s,
                    result_version = result_version + 1,
                    resume_values = NULL,
                    finished_at = now(),
                    updated_at = now(),
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    status_history = status_history || %s::jsonb
                WHERE run_id = %s AND status = 'running' AND execution_generation = %s
                """,
                (
                    json.dumps(result, ensure_ascii=False),
                    _history_entry("succeeded"),
                    UUID(run_id),
                    generation,
                ),
            )
            return cur.rowcount == 1


async def fail_run(
    pool: psycopg.AsyncConnectionPool,
    *,
    run_id: str,
    generation: int,
    error: dict[str, Any],
    backoff_seconds: float,
) -> str | None:
    """失败处理：未耗尽重试回队列（延迟），耗尽转终态 failed。

    返回 'queued' | 'failed' | None（None 表示代次不匹配，拒绝写入）。
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai_runs
                SET status = CASE WHEN attempt_count >= max_attempts THEN 'failed' ELSE 'queued' END,
                    error = %s,
                    run_after = CASE WHEN attempt_count >= max_attempts
                                     THEN run_after
                                     ELSE now() + make_interval(secs => %s) END,
                    finished_at = CASE WHEN attempt_count >= max_attempts THEN now() ELSE NULL END,
                    updated_at = now(),
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    status_history = status_history || %s::jsonb
                WHERE run_id = %s AND status = 'running' AND execution_generation = %s
                RETURNING status
                """,
                (
                    json.dumps(error, ensure_ascii=False),
                    float(backoff_seconds),
                    _history_entry("failed_attempt", error=error),
                    UUID(run_id),
                    generation,
                ),
            )
            row = await cur.fetchone()
            return row[0] if row else None


async def mark_waiting(
    pool: psycopg.AsyncConnectionPool, *, run_id: str, generation: int
) -> bool:
    """图进入 interrupt：释放 worker，等待不占租约。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai_runs
                SET status = 'waiting_input',
                    updated_at = now(),
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    status_history = status_history || %s::jsonb
                WHERE run_id = %s AND status = 'running' AND execution_generation = %s
                """,
                (_history_entry("waiting_input"), UUID(run_id), generation),
            )
            return cur.rowcount == 1


async def queue_resume(
    pool: psycopg.AsyncConnectionPool, *, run_id: str, values: dict[str, Any]
) -> bool:
    """恢复等待中的 Run：仅 waiting_input 可恢复（重复/并发 resume 拒绝）。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai_runs
                SET status = 'queued',
                    resume_values = %s,
                    run_after = now(),
                    updated_at = now(),
                    status_history = status_history || %s::jsonb
                WHERE run_id = %s AND status = 'waiting_input'
                """,
                (
                    json.dumps(values, ensure_ascii=False),
                    _history_entry("queued", reason="resume"),
                    UUID(run_id),
                ),
            )
            return cur.rowcount == 1


async def cancel_run(pool: psycopg.AsyncConnectionPool, *, run_id: str) -> bool:
    """取消：递增 generation 毒化在途提交，迟到的完成会被代次守卫拒绝。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai_runs
                SET status = 'cancelled',
                    finished_at = now(),
                    updated_at = now(),
                    execution_generation = execution_generation + 1,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    status_history = status_history || %s::jsonb
                WHERE run_id = %s AND status IN ('queued', 'running', 'waiting_input')
                """,
                (_history_entry("cancelled"), UUID(run_id)),
            )
            return cur.rowcount == 1


async def requeue_expired_leases(
    pool: psycopg.AsyncConnectionPool, *, limit: int = 50
) -> list[str]:
    """回收租约过期的 running 任务（worker 崩溃恢复）：回队列，下次认领 generation+1。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE ai_runs
                    SET status = 'queued',
                        run_after = now(),
                        updated_at = now(),
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        status_history = status_history || %s::jsonb
                    WHERE run_id IN (
                        SELECT run_id FROM ai_runs
                        WHERE status = 'running' AND lease_expires_at < now()
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING run_id
                    """,
                    (_history_entry("queued", reason="lease_expired"), limit),
                )
                return [str(row[0]) for row in await cur.fetchall()]


async def list_runs(
    pool: psycopg.AsyncConnectionPool,
    *,
    operator_user_id: str,
    statuses: list[str] | None = None,
    capability: str | None = None,
    conversation_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按操作者列出 Run（隔离：只看自己的）。

    支撑 M-07 的"当前 / 历史待处理 / 写入失败"分组视图：调用方按 status 过滤即可，
    不需要额外维护一份状态表。默认按创建时间倒序。
    """
    where = ["operator->>'user_id' = %s"]
    params: list[Any] = [operator_user_id]
    if statuses:
        where.append("status = ANY(%s)")
        params.append(list(statuses))
    if capability:
        where.append("capability = %s")
        params.append(capability)
    if conversation_id:
        where.append("conversation_id = %s")
        params.append(UUID(conversation_id))
    params.extend([limit, offset])
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT * FROM ai_runs
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params),
            )
            return [_row_to_dict(row) for row in await cur.fetchall()]
