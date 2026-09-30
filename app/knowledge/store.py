"""知识授权索引（需求 4.8/4.16；移动端 PRD 对齐 M-10）。

- 知识主档与权限留在 CRM；AI 只存授权索引映射（ai_knowledge_docs）。
- 授权过滤发生在 SQL 检索层——内容进入模型之前已按 user/public 过滤，
  不允许先取无权全文再隐藏引用。
- 停用/撤权直接翻转 status，下一次检索立即从可用集合消失（不等待外部删除）。

M-10 三条硬要求在本层的落实：

1. **只检索已发布当前版本**：版本"最新"由 CRM 发布时用 `is_current` 显式声明，
   AI 不自行推断。旧实现按 `version DESC` 排序选最新，而 version 是 TEXT，
   字符串序会让 "v10" < "v9"，选错版本——已改为 `is_current DESC` 优先，
   仅当 CRM 未标记任何当前版本时才回退到发布时间序（退化为兼容行为，有日志）。
2. **角色×业务线组合不在 AI 侧计算**：授权范围只接受 `public` 或展开后的
   `users`；出现 roles/biz_lines/depts 等 AI 无法判定的维度一律**拒绝同步**
   （失败即关闭），绝不在前端或 AI 侧自行取并集导致超授权。
3. **撤权后不得再次进入模型**：见 `filter_unauthorized_history`。历史会话里
   引用过知识的旧回答，若其引用已被撤权，必须从上下文剔除——否则撤权内容
   会借"会话历史"绕回模型，这是 M-10 明确点出的漏洞。
"""
from __future__ import annotations

import json
import re
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


# AI 能判定的授权维度只有这两个；其余组织维度必须由 CRM 展开为 users 后再同步。
_ALLOWED_SCOPE_KEYS = frozenset({"public", "users"})
# 出现这些键说明调用方试图让 AI 自己算组合——按 M-10「不由前端自行计算」一律拒绝。
_ORG_SCOPE_KEYS = frozenset({"roles", "role", "biz_lines", "biz_line", "depts", "dept", "orgs"})


def validate_scope(scope: dict[str, Any]) -> None:
    """授权范围校验（失败即关闭）。

    只允许 `{"public": true}` 或 `{"users": [...]}`。角色/业务线/部门等维度
    的交集或并集由 CRM 展开为具体 user_id 后再同步；AI 不理解组织结构，
    也绝不能自行取并集（会把"角色 A 或业务线 B"放大成两者皆可见）。
    """
    if not isinstance(scope, dict):
        raise ValueError("knowledge scope 必须是对象")
    unknown = set(scope) - _ALLOWED_SCOPE_KEYS
    if unknown:
        raise ValueError(
            "knowledge scope 只允许 public / users；"
            f"角色、业务线、部门等维度须由 CRM 展开为 users 后再同步（不支持: {', '.join(sorted(unknown))}）"
        )
    if scope.get("public") is True:
        return
    users = scope.get("users")
    if isinstance(users, list) and users and all(isinstance(item, str) and item for item in users):
        return
    raise ValueError('knowledge scope 必须显式声明 {"public": true} 或非空 users 列表（不默认公开）')


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
    is_current: bool = False,
) -> dict[str, Any]:
    """同步写入（发布/更新）；同 (knowledge_id, version) 幂等。

    is_current=True 由 CRM 声明"这是当前版本"：同一 knowledge_id 的其余版本
    会被统一置为 false（唯一索引 uq_knowledge_current 兜底，AI 侧不猜版本）。
    """
    row = await _upsert(
        conn,
        knowledge_id=knowledge_id,
        version=version,
        title=title,
        content=content,
        status=status,
        scope=scope or {},
        published_at=published_at,
        is_current=is_current,
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
    is_current: bool,
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        if is_current:
            # 先把同主档所有版本置为非当前，再写入，保证"最多一个当前版本"
            await cur.execute(
                "UPDATE ai_knowledge_docs SET is_current = false, updated_at = now() "
                "WHERE knowledge_id = %s AND is_current",
                (knowledge_id,),
            )
        await cur.execute(
            """
            INSERT INTO ai_knowledge_docs
                (knowledge_id, version, title, content, status, scope, published_at, is_current)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (knowledge_id, version) DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                status = EXCLUDED.status,
                scope = EXCLUDED.scope,
                published_at = COALESCE(EXCLUDED.published_at, ai_knowledge_docs.published_at),
                is_current = EXCLUDED.is_current,
                disabled_at = CASE WHEN EXCLUDED.status = 'disabled' THEN now() ELSE NULL END,
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
                is_current,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def disable_knowledge(conn: psycopg.AsyncConnection, knowledge_id: str) -> int:
    """停用某个知识主档的全部版本：撤权立即从可用集合生效（需求 4.16 / M-10）。

    同时清掉 is_current 并留痕 disabled_at：撤权的内容不再是"当前版本"，
    且可据此追溯历史会话中哪些旧回答引用了被撤权知识。
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE ai_knowledge_docs "
            "SET status = 'disabled', is_current = false, disabled_at = now(), updated_at = now() "
            "WHERE knowledge_id = %s AND status <> 'disabled'",
            (knowledge_id,),
        )
        return cur.rowcount


def _tokens(text: str) -> set[str]:
    """轻量切词：CJK 走二元切分（中文无空格），拉丁/数字按词。

    只用标准库做字面匹配打分，不引入向量/外部依赖——AI 侧不做语义检索，
    相关性仅用于将"明显无关"的文档排到后面，真正的授权判定与版本选择在 SQL 层。
    """
    lowered = str(text or "").lower()
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", lowered):
        if len(word) >= 2:
            tokens.add(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _relevance_score(query_tokens: set[str], doc: dict[str, Any]) -> int:
    """标题权重高于正文：命中标题记 2 分，正文记 1 分。"""
    if not query_tokens:
        return 0
    title_hits = len(query_tokens & _tokens(str(doc.get("title") or "")))
    content_hits = len(query_tokens & _tokens(str(doc.get("content") or "")))
    return title_hits * 2 + content_hits


async def search_authorized(
    conn: psycopg.AsyncConnection,
    *,
    user_id: str,
    limit: int = 5,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """检索已发布、且该用户有权的**当前版本**（过滤在 SQL 层，先于内容进入模型）。

    排序：is_current 优先 → published_at → created_at。
    version 是 TEXT，绝不用它排序选"最新"（"v10" 会小于 "v9"）。
    CRM 未标记任何当前版本时退化为发布时间序，此时不是 M-10 所要求的严格语义，
    由调用方（CRM 同步）补齐 is_current 后自动恢复。

    传入 query 时按字面相关度重排：先取一个更大的候选池，再按命中打分取前 limit 条。
    相关性只是排序手段，**不参与授权判定**——无权文档在 SQL 层已被排除，
    也不会因为"相关"被放行。全部零命中时保持原序（不改变既有行为）。
    """
    pool_size = limit if not query else max(limit * 6, 20)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT DISTINCT ON (knowledge_id)
                knowledge_id, version, title, content, status, scope, published_at, is_current
            FROM ai_knowledge_docs
            WHERE status = 'published' AND {_scope_filter_sql()}
            ORDER BY knowledge_id, is_current DESC, published_at DESC NULLS LAST, created_at DESC
            LIMIT %s
            """,
            (json.dumps([user_id]), pool_size),
        )
        rows = [dict(row) for row in await cur.fetchall()]
    if not query or not rows:
        return rows[:limit] if query else rows
    query_tokens = _tokens(query)
    scored = [( _relevance_score(query_tokens, row), index, row) for index, row in enumerate(rows)]
    if not any(score for score, _, _ in scored):
        return rows[:limit]  # 全不相关：维持原序，不假装检索到东西
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:limit]]


async def authorized_knowledge_ids(
    conn: psycopg.AsyncConnection, *, user_id: str, knowledge_ids: list[str]
) -> set[str]:
    """在给定 id 集合中，筛出当前仍"已发布且本人有权"的知识 id。"""
    if not knowledge_ids:
        return set()
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT DISTINCT knowledge_id FROM ai_knowledge_docs
            WHERE status = 'published' AND {_scope_filter_sql()}
              AND knowledge_id = ANY(%s)
            """,
            (json.dumps([user_id]), list(knowledge_ids)),
        )
        return {row[0] for row in await cur.fetchall()}


def cited_knowledge_ids(message: dict[str, Any]) -> list[str]:
    """取出一条会话消息引用过的知识 id（写入时由 executor 记录在 meta）。"""
    meta = message.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            return []
    if not isinstance(meta, dict):
        return []
    cited = meta.get("knowledge_refs") or meta.get("cited_knowledge") or []
    if not isinstance(cited, list):
        return []
    ids: list[str] = []
    for item in cited:
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
        elif isinstance(item, dict) and str(item.get("ref_id") or "").strip():
            ids.append(str(item["ref_id"]).strip())
    return ids


async def filter_unauthorized_history(
    conn: psycopg.AsyncConnection, *, user_id: str, history: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """撤权拦截：剔除引用了"已撤权/已无权"知识的旧回答（M-10）。

    历史会作为 `[H]` 行进入模型。若旧回答里引用过知识，而该知识后来被撤权，
    不加拦截的话这些内容会借"会话历史"再次进入模型——等于撤权无效。

    返回 (可安全使用的历史, 被剔除消息的引用 id 列表)。
    只剔 assistant 消息：用户自己说的话不是授权内容，保留以免上下文断裂。
    """
    keep: list[dict[str, Any]] = []
    dropped: list[str] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        cited = cited_knowledge_ids(item)
        is_assistant = str(item.get("role") or "") == "assistant"
        if not (is_assistant and cited):
            keep.append(item)
            continue
        still_ok = await authorized_knowledge_ids(conn, user_id=user_id, knowledge_ids=cited)
        missing = [kid for kid in cited if kid not in still_ok]
        if missing:
            dropped.extend(missing)
            continue
        keep.append(item)
    return keep, dropped
