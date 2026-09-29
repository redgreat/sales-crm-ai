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

from app.util import new_uuid


def thread_id_for_conversation(conversation_id: str) -> str:
    return f"conv:{conversation_id}"


def _conv_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if isinstance(out.get("conversation_id"), UUID):
        out["conversation_id"] = str(out["conversation_id"])
    return out


async def create_conversation(
    pool: psycopg.AsyncConnectionPool,
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
    pool: psycopg.AsyncConnectionPool, conversation_id: str
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
    pool: psycopg.AsyncConnectionPool, conversation_id: str
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


async def append_message(
    pool: psycopg.AsyncConnectionPool,
    *,
    conversation_id: str,
    role: str,
    content: str,
    run_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """追加消息；seq 在同会话内串行分配（事务保证），并发提交会串行化。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (conversation_id,))
                await cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM ai_messages WHERE conversation_id = %s",
                    (UUID(conversation_id),),
                )
                row = await cur.fetchone()
                seq = (row["max_seq"] if row else 0) + 1
                await cur.execute(
                    """
                    INSERT INTO ai_messages (conversation_id, run_id, seq, role, content, meta)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
                    """,
                    (
                        UUID(conversation_id),
                        UUID(run_id) if run_id else None,
                        seq,
                        role,
                        content,
                        json.dumps(meta or {}, ensure_ascii=False),
                    ),
                )
                created = await cur.fetchone()
                assert created is not None
                return dict(created)


async def list_messages(
    pool: psycopg.AsyncConnectionPool, conversation_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM ai_messages WHERE conversation_id = %s
                ORDER BY seq LIMIT %s
                """,
                (UUID(conversation_id), limit),
            )
            return [dict(row) for row in await cur.fetchall()]
