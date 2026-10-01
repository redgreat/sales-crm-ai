"""会话与消息存储：CRM 用户/对象绑定 conversationId，映射 LangGraph thread_id。

历史与结构化补参状态分开：消息只存脱敏文本与业务元数据，
补参状态在图 checkpoint（结构化 state），供应商 usage 一律不落库。
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.util import new_uuid


def thread_id_for_conversation(conversation_id: str) -> str:
    return f"conv:{conversation_id}"


def _conv_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if isinstance(out.get("conversation_id"), UUID):
        out["conversation_id"] = str(out["conversation_id"])
    return out


async def create_conversation(
    pool: AsyncConnectionPool,
    *,
    crm_user_id: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
) -> dict[str, Any]:
    conversation_id = new_uuid()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO ai_conversations (conversation_id, crm_user_id, subject_type, subject_id, thread_id)
                VALUES (%s, %s, %s, %s, %s) RETURNING *
                """,
                (
                    conversation_id,
                    crm_user_id,
                    subject_type,
                    subject_id,
                    thread_id_for_conversation(str(conversation_id)),
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            return _conv_dict(row)


async def get_conversation(
    pool: AsyncConnectionPool, conversation_id: str
) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM ai_conversations WHERE conversation_id = %s",
                (UUID(conversation_id),),
            )
            row = await cur.fetchone()
            return _conv_dict(row) if row else None


async def close_conversation(
    pool: AsyncConnectionPool, conversation_id: str
) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai_conversations SET status = 'closed', updated_at = now()
                WHERE conversation_id = %s AND status = 'active'
                """,
                (UUID(conversation_id),),
            )
            return cur.rowcount == 1


async def expire_stale_conversations(
    pool: AsyncConnectionPool, *, ttl_hours: int
) -> int:
    """把超过 TTL 无活动的活跃会话标记为 expired。

    M-01 红线：会话过期只影响「能否继续在同一会话续问」（恢复语义），
    绝不改变任何候选业务状态——候选生命周期归 CRM（后端 05 §10）。
    ttl_hours <= 0 表示禁用（不做过期清理）。
    """
    if ttl_hours <= 0:
        return 0
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ai_conversations SET status = 'expired', updated_at = now()
                WHERE status = 'active'
                  AND updated_at < now() - make_interval(hours => %s)
                """,
                (ttl_hours,),
            )
            return cur.rowcount


async def append_message(
    pool: AsyncConnectionPool,
    *,
    conversation_id: str,
    role: str,
    content: str,
    run_id: str | None = None,
    meta: dict[str, Any] | None = None,
    client_key: str | None = None,
) -> dict[str, Any]:
    """追加消息；seq 在同会话内串行分配（事务保证），并发提交会串行化。

    client_key 是客户端幂等键（同会话唯一索引兜底）：弱网重复提交命中已有
    消息时返回该消息而不新插（M-07：重复提交返回原 Run，不产生新结果）。
    每次消息活动刷新会话 updated_at——TTL 过期按最后活动时间计。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (conversation_id,))
                if client_key:
                    await cur.execute(
                        "SELECT * FROM ai_messages WHERE conversation_id = %s AND client_key = %s",
                        (UUID(conversation_id), client_key),
                    )
                    existing = await cur.fetchone()
                    if existing is not None:
                        return dict(existing)
                await cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM ai_messages WHERE conversation_id = %s",
                    (UUID(conversation_id),),
                )
                row = await cur.fetchone()
                seq = (row["max_seq"] if row else 0) + 1
                await cur.execute(
                    """
                    INSERT INTO ai_messages (conversation_id, run_id, seq, role, content, meta, client_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
                    """,
                    (
                        UUID(conversation_id),
                        UUID(run_id) if run_id else None,
                        seq,
                        role,
                        content,
                        json.dumps(meta or {}, ensure_ascii=False),
                        client_key,
                    ),
                )
                created = await cur.fetchone()
                assert created is not None
                await cur.execute(
                    "UPDATE ai_conversations SET updated_at = now() WHERE conversation_id = %s",
                    (UUID(conversation_id),),
                )
                return dict(created)


async def find_message_by_client_key(
    source: AsyncConnectionPool | psycopg.AsyncConnection,
    conversation_id: str,
    client_key: str,
) -> dict[str, Any] | None:
    """按客户端幂等键查消息：命中说明同一条消息已受理（返回其 Run 供幂等重放）。"""

    async def _query(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM ai_messages WHERE conversation_id = %s AND client_key = %s",
                (UUID(conversation_id), client_key),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    if isinstance(source, AsyncConnectionPool):
        async with source.connection() as conn:
            return await _query(conn)
    return await _query(source)


async def _fetch_messages(
    conn: psycopg.AsyncConnection,
    conversation_id: str,
    limit: int,
    before_seq: int | None,
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        if before_seq is not None:
            # 分页：before_seq 之前的更早消息，仍按时间正序返回
            await cur.execute(
                """
                SELECT * FROM (
                    SELECT * FROM ai_messages
                    WHERE conversation_id = %s AND seq < %s
                    ORDER BY seq DESC LIMIT %s
                ) recent ORDER BY seq ASC
                """,
                (UUID(conversation_id), before_seq, limit),
            )
        else:
            # 最近窗口：取最新 N 条再反转为时间正序。
            # 历史装配与前端恢复要的都是"最近的语境"（旧实现 ORDER BY seq LIMIT
            # 拿到的是最早 N 条，长会话会丢失全部近期上下文）。
            await cur.execute(
                """
                SELECT * FROM (
                    SELECT * FROM ai_messages WHERE conversation_id = %s
                    ORDER BY seq DESC LIMIT %s
                ) recent ORDER BY seq ASC
                """,
                (UUID(conversation_id), limit),
            )
        return [dict(row) for row in await cur.fetchall()]


async def list_messages(
    source: AsyncConnectionPool | psycopg.AsyncConnection,
    conversation_id: str,
    *,
    limit: int = 100,
    before_seq: int | None = None,
) -> list[dict[str, Any]]:
    """列出会话消息：默认最近 `limit` 条（时间正序），`before_seq` 向更早翻页。

    同时接受**连接池**或**单条连接**：executor 装配上下文时已持有一条连接，
    复用它可以少一次借还往返，不必为此再开一条。
    """
    if isinstance(source, AsyncConnectionPool):
        async with source.connection() as conn:
            return await _fetch_messages(conn, conversation_id, limit, before_seq)
    return await _fetch_messages(source, conversation_id, limit, before_seq)
