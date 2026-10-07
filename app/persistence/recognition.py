"""识别产物持久化（P5-INPUT 任务 2）。

ai_file_recognitions：识别输出、供应商任务引用、来源锚点与配置版本。
识别与 LLM 整理是两个阶段——这里只存识别；重试整理必须命中已成功的识别，
不重复调用供应商（需求 12.3：ASR/OCR 成功后重试 LLM 复用原输出）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class RecognitionRepoError(RuntimeError):
    """识别产物状态流转非法。"""


def _row_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row = dict(row)
    for key in ("evidence", "error"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    row["recognition_id"] = str(row["recognition_id"])
    row["run_id"] = str(row["run_id"]) if row.get("run_id") else None
    return row


async def create(
    pool: AsyncConnectionPool,
    *,
    run_id: str | None,
    file_id: str,
    source_version: int,
    processing: str,
    provider: str = "",
    provider_task_id: str = "",
    config_version: str,
) -> dict[str, Any]:
    """登记一次识别（status=running）。崩溃后可按 run/file 追溯。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO ai_file_recognitions
                    (recognition_id, run_id, file_id, source_version, processing,
                     provider, provider_task_id, config_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    uuid.uuid4(),
                    uuid.UUID(run_id) if run_id else None,
                    file_id,
                    source_version,
                    processing,
                    provider,
                    provider_task_id,
                    config_version,
                ),
            )
            return _row_dict(await cur.fetchone())  # type: ignore[return-value]


async def get_reusable(
    pool: AsyncConnectionPool,
    *,
    file_id: str,
    source_version: int,
    processing: str,
    config_version: str,
) -> dict[str, Any] | None:
    """命中即可复用的成功结果（文件+版本+处理类型+配置版本全一致）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM ai_file_recognitions
                WHERE file_id = %s AND source_version = %s AND processing = %s
                  AND config_version = %s AND status = 'succeeded'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (file_id, source_version, processing, config_version),
            )
            return _row_dict(await cur.fetchone())


async def get_resumable_task(
    pool: AsyncConnectionPool, *, file_id: str, source_version: int, processing: str,
    config_version: str,
) -> dict[str, Any] | None:
    """Find an ASR task whose provider id was saved before a worker stopped."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT * FROM ai_file_recognitions
                   WHERE file_id = %s AND source_version = %s AND processing = %s
                     AND config_version = %s AND status = 'running' AND provider_task_id <> ''
                   ORDER BY created_at DESC LIMIT 1""",
                (file_id, source_version, processing, config_version),
            )
            return _row_dict(await cur.fetchone())


async def set_provider_task(pool: AsyncConnectionPool, recognition_id: str, provider_task_id: str) -> None:
    """提交供应商任务后先记 task_id：进程崩溃后可凭它查原任务，避免重复外部调用。"""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ai_file_recognitions SET provider_task_id = %s, updated_at = now() "
                "WHERE recognition_id = %s AND status = 'running'",
                (provider_task_id, uuid.UUID(recognition_id)),
            )


async def complete(
    pool: AsyncConnectionPool,
    recognition_id: str,
    *,
    raw_text: str,
    anchored_text: str = "",
    evidence: list[dict[str, Any]] | None = None,
    duration_ms: int | None = None,
    provider_task_id: str | None = None,
) -> dict[str, Any]:
    """标记成功并落文本/锚点。并发下同键第二次成功撞部分唯一索引时返回既有行（幂等）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    """
                    UPDATE ai_file_recognitions
                    SET status = 'succeeded', raw_text = %s, anchored_text = %s,
                        evidence = %s, duration_ms = %s,
                        provider_task_id = COALESCE(%s, provider_task_id),
                        completed_at = now(), updated_at = now()
                    WHERE recognition_id = %s AND status = 'running'
                    RETURNING *
                    """,
                    (
                        raw_text,
                        anchored_text,
                        Jsonb(evidence or []),
                        duration_ms,
                        provider_task_id,
                        uuid.UUID(recognition_id),
                    ),
                )
                updated = await cur.fetchone()
                if updated is None:
                    raise RecognitionRepoError("识别已取消或终止，迟到结果不能完成")
                return _row_dict(updated)  # type: ignore[return-value]
            except pg_errors.UniqueViolation:
                # 另一执行者已完成同键识别：丢弃本行结果，复用既有成功行
                pass
    await _cancel_if_running(pool, recognition_id)
    row = await get_reusable_by_id(pool, recognition_id)
    if row is None:
        raise RecognitionRepoError("完成识别失败且无法定位既有成功结果")
    return row


async def _cancel_if_running(pool: AsyncConnectionPool, recognition_id: str) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ai_file_recognitions SET status = 'cancelled', updated_at = now() "
                "WHERE recognition_id = %s AND status = 'running'",
                (uuid.UUID(recognition_id),),
            )


async def get_reusable_by_id(pool: AsyncConnectionPool, recognition_id: str) -> dict[str, Any] | None:
    """按本行键查可复用成功结果（含本行已成功或并发者成功两种情况）。"""
    mine = await get_by_id(pool, recognition_id)
    if mine is None:
        return None
    return await get_reusable(
        pool,
        file_id=mine["file_id"],
        source_version=mine["source_version"],
        processing=mine["processing"],
        config_version=mine["config_version"],
    )


async def fail(pool: AsyncConnectionPool, recognition_id: str, *, error: dict[str, Any]) -> dict[str, Any]:
    """阶段失败落 {stage, code, message}；不写敏感 URL。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE ai_file_recognitions
                SET status = 'failed', error = %s, completed_at = now(), updated_at = now()
                WHERE recognition_id = %s AND status = 'running'
                RETURNING *
                """,
                (Jsonb(error), uuid.UUID(recognition_id)),
            )
            return _row_dict(await cur.fetchone())  # type: ignore[return-value]


async def cancel(pool: AsyncConnectionPool, recognition_id: str) -> bool:
    """Run 取消时把 running 识别置 cancelled；迟到结果因状态守卫不再被复用。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE ai_file_recognitions SET status = 'cancelled', updated_at = now()
                WHERE recognition_id = %s AND status = 'running'
                RETURNING recognition_id
                """,
                (uuid.UUID(recognition_id),),
            )
            return await cur.fetchone() is not None


async def cancel_by_run(pool: AsyncConnectionPool, run_id: str) -> int:
    """Poison in-flight recognition rows when their owning Run is cancelled."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ai_file_recognitions SET status = 'cancelled', updated_at = now() "
                "WHERE run_id = %s AND status = 'running'",
                (uuid.UUID(run_id),),
            )
            return cur.rowcount


async def get_by_id(pool: AsyncConnectionPool, recognition_id: str) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM ai_file_recognitions WHERE recognition_id = %s",
                (uuid.UUID(recognition_id),),
            )
            return _row_dict(await cur.fetchone())


async def list_by_file(pool: AsyncConnectionPool, *, file_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """按文件回溯识别历史（诊断/审计）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM ai_file_recognitions WHERE file_id = %s ORDER BY created_at DESC LIMIT %s",
                (file_id, limit),
            )
            return [_row_dict(row) for row in await cur.fetchall()]  # type: ignore[arg-type]
