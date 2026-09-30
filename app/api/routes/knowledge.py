"""知识路由：CRM 知识发布/停用的授权同步（需求 4.16）。

CRM 侧在知识发布/停用/撤权时调用本端点同步索引映射；失败可重试
（同 knowledge_id+version 的 upsert 幂等）。服务间签名认证；权限判
定留在 CRM，AI 只存索引，不做组织结构解析（dept/角色需在 CRM 展开
为 user_ids 或 public 标记）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import require_operator
from app.auth import OperatorContext
from app.errors import NotFound, ValidationFailed
from app.knowledge import disable_knowledge, upsert_document, validate_scope

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


class KnowledgeDocBody(BaseModel):
    knowledge_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    title: str = Field(default="", max_length=300)
    content: str = Field(default="", max_length=20000)
    status: str = Field(default="published", pattern="^(published|disabled)$")
    scope: dict[str, Any] = Field(default_factory=dict)
    published_at: str | None = None
    # 由 CRM 声明"当前版本"：M-10 要求只检索已发布当前版本，AI 不自行推断版本新旧
    is_current: bool = False


def _validate_scope(scope: dict[str, Any]) -> None:
    """授权范围校验：失败即关闭（委托 store.validate_scope，避免两处规则漂移）。"""
    try:
        validate_scope(scope)
    except ValueError as exc:
        raise ValidationFailed(str(exc), details={"field": "scope"}) from exc


@router.post("/documents", status_code=201)
async def sync_document(
    body: KnowledgeDocBody, request: Request, operator: OperatorContext = Depends(require_operator)
) -> dict[str, Any]:
    """发布/更新知识文档（幂等：同 knowledge_id+version 重复同步无副作用）。"""
    del operator  # 同步权限由 CRM 服务端判定（主档权限留 CRM）
    _validate_scope(body.scope)
    if body.status == "published" and not body.content.strip():
        raise ValidationFailed("发布版 content 不能为空", details={"knowledge_id": body.knowledge_id})
    async with request.app.state.pool.connection() as conn:
        row = await upsert_document(
            conn,
            knowledge_id=body.knowledge_id,
            version=body.version,
            title=body.title,
            content=body.content,
            status=body.status,
            scope=body.scope,
            published_at=body.published_at,
            is_current=body.is_current,
        )
    return {
        "knowledge_id": row["knowledge_id"],
        "version": row["version"],
        "status": row["status"],
        "is_current": bool(row.get("is_current")),
        "synced": True,
    }


@router.post("/documents/{knowledge_id}/disable")
async def disable_document(
    knowledge_id: str, request: Request, operator: OperatorContext = Depends(require_operator)
) -> dict[str, Any]:
    """停用某知识主档的全部版本：立即从可用检索集合中消失（需求 4.16 撤权语义）。"""
    del operator
    async with request.app.state.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM ai_knowledge_docs WHERE knowledge_id = %s LIMIT 1", (knowledge_id,)
            )
            if await cur.fetchone() is None:
                raise NotFound("知识条目不存在")
        affected = await disable_knowledge(conn, knowledge_id)
    return {"knowledge_id": knowledge_id, "status": "disabled", "disabled_versions": affected}
