"""会话路由：对象绑定会话、消息、多轮补参入口。

每条消息对应独立 Run（requestId/runId 每轮独立），同一会话共享 thread；
会话处于等待补参时，新消息转为对等待 Run 的恢复请求（先重新鉴权、后校验状态）。
"""
from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import require_operator
from app.auth import OperatorContext
from app.errors import Forbidden, NotFound, ValidationFailed
from app.graphs.builder import graph_registry_version
from app.graphs.interrupts import get_pending_interrupt
from app.persistence import conversations as conversations_repo
from app.persistence import runs as runs_repo

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


class CreateConversationBody(BaseModel):
    subject_type: str | None = Field(default=None, max_length=50)
    subject_id: str | None = Field(default=None, max_length=100)


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    idempotency_key: str | None = Field(default=None, max_length=200)


def _conv_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise NotFound("会话不存在") from exc


async def _require_owned_conversation(
    request: Request, conversation_id: str, operator: OperatorContext
) -> dict[str, Any]:
    conversation = await conversations_repo.get_conversation(request.app.state.pool, conversation_id)
    if conversation is None:
        raise NotFound("会话不存在")
    if conversation["crm_user_id"] != operator.user_id:
        raise Forbidden("无权访问该会话")
    return conversation


@router.post("", status_code=201)
async def create_conversation(
    body: CreateConversationBody, request: Request, operator: OperatorContext = Depends(require_operator)
) -> dict[str, Any]:
    conversation = await conversations_repo.create_conversation(
        request.app.state.pool,
        crm_user_id=operator.user_id,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
    )
    return {
        "conversation_id": conversation["conversation_id"],
        "thread_id": conversation["thread_id"],
        "status": conversation["status"],
    }


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    request: Request,
    operator: OperatorContext = Depends(require_operator),
    limit: int = Query(default=100, ge=1, le=500),
    before_seq: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """会话消息：默认返回最近 `limit` 条（时间正序）；`before_seq` 向更早翻页。

    移动端收起/刷新恢复只需最近窗口；向上翻历史时用上一页最小 seq 作 before_seq。
    """
    await _require_owned_conversation(request, conversation_id, operator)
    messages = await conversations_repo.list_messages(
        request.app.state.pool, conversation_id, limit=limit, before_seq=before_seq
    )
    return {
        "conversation_id": conversation_id,
        "window": {"limit": limit, "before_seq": before_seq},
        "messages": [
            {
                "seq": m["seq"],
                "role": m["role"],
                "content": m["content"],
                "run_id": m.get("run_id"),
                "meta": m.get("meta", {}),
                "created_at": m.get("created_at"),
            }
            for m in messages
        ],
    }


def _client_message_key(conversation_id: str, client_key: str) -> str:
    """客户端幂等键命名空间化：键只在会话内唯一，避免与其他能力的全局键冲突。"""
    return f"msg:{conversation_id}:{client_key}"


@router.post("/{conversation_id}/messages", status_code=202)
async def post_message(
    conversation_id: str, body: MessageBody, request: Request, operator: OperatorContext = Depends(require_operator)
) -> dict[str, Any]:
    conversation = await _require_owned_conversation(request, conversation_id, operator)
    if conversation["status"] != "active":
        raise ValidationFailed("会话已结束，不能继续发送消息")

    client_key = (
        _client_message_key(conversation_id, body.idempotency_key.strip())
        if (body.idempotency_key or "").strip()
        else None
    )

    # 消息级幂等前置检查（M-07 弱网重复提交）：同 key 已受理过则直接返回原 Run，
    # 不再进入 waiting/新 Run 分支——恢复完成后重试不应产生重复的抽取 Run。
    if client_key:
        seen = await conversations_repo.find_message_by_client_key(
            request.app.state.pool, conversation_id, client_key
        )
        if seen is not None and seen.get("run_id"):
            run = await runs_repo.get_run(request.app.state.pool, str(seen["run_id"]))
            if run is not None:
                return {
                    "mode": "replay",
                    "run_id": str(run["run_id"]),
                    "status": run["status"],
                    "idempotent_replay": True,
                }

    waiting = await runs_repo.find_latest_run_by_status(
        request.app.state.pool, conversation_id=conversation_id, status="waiting_input"
    )
    if waiting is not None:
        # 会话在等待补参：本条消息作为恢复应答（等待 Run 独立幂等键，恢复队列化）
        accepted = await runs_repo.queue_resume(
            request.app.state.pool,
            run_id=str(waiting["run_id"]),
            values={"text": body.text},
        )
        if accepted:
            await conversations_repo.append_message(
                request.app.state.pool,
                conversation_id=conversation_id,
                role="user",
                content=body.text,
                run_id=str(waiting["run_id"]),
                client_key=client_key,
            )
            return {"mode": "resume", "run_id": str(waiting["run_id"]), "status": "queued"}
        # 恢复请求刚被并发处理：落库后按新消息走（下方新建 Run）

    settings = request.app.state.settings
    # 每轮消息独立 Run：客户端提供幂等键用客户端的（同 key 重试幂等重放）；
    # 未提供则一次性键——绝不能用固定会话键，否则第二条消息会重放第一个 Run。
    idempotency_key = client_key or f"msg:{conversation_id}:auto:{uuid.uuid4().hex}"
    run, created = await runs_repo.create_run(
        request.app.state.pool,
        idempotency_key=idempotency_key,
        capability="communication.extract",
        input_payload={"text": body.text},
        operator={"user_id": operator.user_id, "user_name": operator.user_name},
        thread_id=conversation["thread_id"],
        conversation_id=conversation_id,
        graph_version=graph_registry_version()["communication.extract"],
        prompt_version="extract-prompt@1",
        max_attempts=settings.worker.max_attempts,
    )
    await conversations_repo.append_message(
        request.app.state.pool,
        conversation_id=conversation_id,
        role="user",
        content=body.text,
        run_id=str(run["run_id"]),
        client_key=client_key,
    )
    return {"mode": "new_run", "run_id": run["run_id"], "status": run["status"], "idempotent_replay": not created}


@router.get("/{conversation_id}/pending")
async def get_pending(
    conversation_id: str, request: Request, operator: OperatorContext = Depends(require_operator)
) -> dict[str, Any]:
    """当前等待补参的 Run 及追问内容（供补参界面渲染）。"""
    await _require_owned_conversation(request, conversation_id, operator)
    waiting = await runs_repo.find_latest_run_by_status(
        request.app.state.pool, conversation_id=conversation_id, status="waiting_input"
    )
    if waiting is None:
        return {"conversation_id": conversation_id, "waiting": False}
    graph = request.app.state.graphs.get(waiting["capability"])
    payload = await get_pending_interrupt(graph, waiting["thread_id"]) if graph else None
    return {
        "conversation_id": conversation_id,
        "waiting": True,
        "run_id": str(waiting["run_id"]),
        "questions": (payload or {}).get("questions", []),
        "state_version": (payload or {}).get("state_version"),
    }


@router.post("/{conversation_id}/close")
async def close_conversation(
    conversation_id: str, request: Request, operator: OperatorContext = Depends(require_operator)
) -> dict[str, Any]:
    await _require_owned_conversation(request, conversation_id, operator)
    closed = await conversations_repo.close_conversation(request.app.state.pool, conversation_id)
    if not closed:
        raise ValidationFailed("会话已结束")
    return {"conversation_id": conversation_id, "status": "closed"}
