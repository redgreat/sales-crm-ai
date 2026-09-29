"""会话路由：对象绑定会话、消息、多轮补参入口。

每条消息对应独立 Run（requestId/runId 每轮独立），同一会话共享 thread；
会话处于等待补参时，新消息转为对等待 Run 的恢复请求（先重新鉴权、后校验状态）。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
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
    conversation_id: str, request: Request, operator: OperatorContext = Depends(require_operator)
) -> dict[str, Any]:
    await _require_owned_conversation(request, conversation_id, operator)
    messages = await conversations_repo.list_messages(request.app.state.pool, conversation_id)
    return {
        "conversation_id": conversation_id,
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


@router.post("/{conversation_id}/messages", status_code=202)
async def post_message(
    conversation_id: str, body: MessageBody, request: Request, operator: OperatorContext = Depends(require_operator)
) -> dict[str, Any]:
    conversation = await _require_owned_conversation(request, conversation_id, operator)
    if conversation["status"] != "active":
        raise ValidationFailed("会话已结束，不能继续发送消息")

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
            )
            return {"mode": "resume", "run_id": str(waiting["run_id"]), "status": "queued"}
        # 恢复请求刚被并发处理：落库后按新消息走（下方新建 Run）

    settings = request.app.state.settings
    idempotency_key = body.idempotency_key or f"conv:{conversation_id}:{operator.user_id}"
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
