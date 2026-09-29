"""知识授权索引（需求 4.8/4.16）：

- 知识主档与权限留在 CRM；AI 只存授权索引映射（ai_knowledge_docs）。
- 授权过滤发生在 SQL 检索层——内容进入模型之前已按 user/public 过滤，
  不允许先取无权全文再隐藏引用。
- 停用/撤权直接翻转 status，下一次检索立即从可用集合消失（不等待外部删除）。
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row


def doc_as_fact(doc: dict[str, Any]) -> dict[str, Any]:
    """把知识索引条目转成图事实行（citations 会带 id/type/version）。"""
    title = str(doc.get("title") or "").strip()
    content = str(doc.get("content") or "").strip()
    text = f"{title}: {content}" if title else content
    return {
        "type": "knowledge",
        "id": str(doc["knowledge_id"]),
        "version": str(doc.get("version") or ""),
        "text": text,
    }


def _scope_filter_sql() -> str:
    # public 或 users 数组包含调用者；scope 形如 {"public": true} / {"users": [...]}
    return "(scope->>'public' = 'true' OR scope->'users' @> %s::jsonb)"


async def upsert_document(
    conn: psycopg.AsyncConnection,
    *,
    knowledge_id: str,
    version: str,
    title: str = "",
    content: str = "",
    status: str = "published",
    scope: dict[str, Any] | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    """同步写入（发布/更新）；同 (knowledge_id, version) 幂等。"""
    row = await _upsert(
        conn,
        knowledge_id=knowledge_id,
        version=version,
        title=title,
        content=content,
        status=status,
        scope=scope or {},
        published_at=published_at,
    )
    return row


async def _upsert(
    conn: psycopg.AsyncConnection,
    *,
    knowledge_id: str,
    version: str,
    title: str,
    content: str,
    status: str,
    scope: dict[str, Any],
    published_at: str | None,
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO ai_knowledge_docs
                (knowledge_id, version, title, content, status, scope, published_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (knowledge_id, version) DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                status = EXCLUDED.status,
                scope = EXCLUDED.scope,
                published_at = COALESCE(EXCLUDED.published_at, ai_knowledge_docs.published_at),
                updated_at = now()
            RETURNING *
            """,
            (
                knowledge_id,
                version,
                title,
                content,
                status,
                json.dumps(scope, ensure_ascii=False),
                published_at,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def disable_knowledge(conn: psycopg.AsyncConnection, knowledge_id: str) -> int:
    """停用某个知识主档的全部版本：撤权立即从可用集合生效（需求 4.16）。"""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE ai_knowledge_docs SET status = 'disabled', updated_at = now() "
            "WHERE knowledge_id = %s AND status <> 'disabled'",
            (knowledge_id,),
        )
        return cur.rowcount


async def search_authorized(
    conn: psycopg.AsyncConnection,
    *,
    user_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """检索已发布且该用户有权的知识（过滤在 SQL 层，先于内容进入模型）。

    同一 knowledge_id 取 published_at 最新的已发布版本；返回条数受 limit 约束。
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT DISTINCT ON (knowledge_id)
                knowledge_id, version, title, content, status, scope, published_at
            FROM ai_knowledge_docs
            WHERE status = 'published' AND {_scope_filter_sql()}
            ORDER BY knowledge_id, published_at DESC NULLS LAST, version DESC
            LIMIT %s
            """,
            (json.dumps([user_id]), limit),
        )
        return [dict(row) for row in await cur.fetchall()]
