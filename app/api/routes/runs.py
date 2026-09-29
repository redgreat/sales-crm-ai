"""Run 路由：创建（幂等）、查询、取消、恢复。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import require_operator
from app.auth import OperatorContext
from app.capabilities import CapabilitySpec, get_capability, pregen_idempotency_key
from app.errors import (
    CapabilityUnknown,
    Forbidden,
    NotFound,
    RunNotResumable,
    StateVersionConflict,
    ValidationFailed,
)
from app.graphs.interrupts import get_pending_interrupt
from app.persistence import conversations as conversations_repo
from app.persistence import runs as runs_repo
from app.util import utcnow

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


class CreateRunBody(BaseModel):
    capability: str
    input: dict[str, Any] = Field(default_factory=dict)
    # 预生成能力（日报底稿/今日任务/主管关注）可省略：由 input.schedule_key 派生。
    idempotency_key: str | None = Field(default=None, max_length=200)
    conversation_id: str | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class ResumeBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    state_version: int


def _serialize(run: dict[str, Any]) -> dict[str, Any]:
    spec = get_capability(run["capability"])
    return {
        "run_id": run["run_id"],
        "capability": run["capability"],
        "status": run["status"],
        "idempotency_key": run["idempotency_key"],
        "conversation_id": run.get("conversation_id"),
        "attempt_count": run["attempt_count"],
        "max_attempts": run["max_attempts"],
        "graph_version": run["graph_version"],
        "prompt_version": run.get("prompt_version"),
        "read_only": spec.read_only if spec else None,
        "pregen": spec.pregen if spec else None,
        "result": run.get("result"),
        "error": run.get("error"),
        "status_history": run.get("status_history", []),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
    }


def _require_owned_run(run: dict[str, Any] | None, operator: OperatorContext) -> dict[str, Any]:
    if run is None:
        raise NotFound("Run 不存在")
    owner = (run.get("operator") or {}).get("user_id")
    if owner != operator.user_id:
        raise Forbidden("无权访问该 Run")
    return run


def _dotted(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _validate_capability_input(spec: CapabilitySpec, body: CreateRunBody) -> None:
    """按能力契约校验入参（登记表是唯一真源，未登记字段不静默忽略）。

    - 抽取型：需要 input.text；
    - 只读分析型：需要 CRM 按权限装配的 input.facts（每项有稳定 id 供引用核对）；
    - 预生成型：额外需要 beneficiary.user_id 与 schedule_key（调度幂等）。
    """
    payload = body.input or {}
    missing = [path for path in spec.required_input_fields if not _dotted(payload, path)]
    if missing:
        raise ValidationFailed(
            f"缺少必填输入: {', '.join(missing)}",
            details={"capability": spec.name, "missing": missing},
        )
    if spec.requires_text and not str(payload.get("text", "")).strip():
        raise ValidationFailed(
            "input.text 不能为空",
            details={"capability": spec.name, "field": "text"},
        )
    if spec.requires_facts:
        facts = payload.get("facts")
        if not isinstance(facts, list) or not facts:
            raise ValidationFailed(
                "input.facts 不能为空（必须由 CRM 按权限装配后随 Run 传入）",
                details={"capability": spec.name},
            )
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict) or not str(fact.get("id", "")).strip():
                raise ValidationFailed(
                    f"input.facts[{index}] 必须是对象且带稳定 id",
                    details={"capability": spec.name, "index": index},
                )
    if spec.pregen:
        if not _dotted(payload, "beneficiary.user_id"):
            raise ValidationFailed(
                "预生成能力必须显式指定受益人 beneficiary.user_id",
                details={"capability": spec.name},
            )
        schedule_key = str(_dotted(payload, "schedule_key") or "").strip()
        if len(schedule_key) < 8:
            raise ValidationFailed(
                "预生成能力必须提供 input.schedule_key（至少 8 位，由 CRM 按受益人/窗口计算）",
                details={"capability": spec.name},
            )


def _resolve_idempotency_key(spec: CapabilitySpec, body: CreateRunBody) -> tuple[str, str]:
    """返回 (幂等键, 来源)；预生成一律由 schedule_key 派生，避免两端重复调度。"""
    if spec.pregen:
        schedule_key = str(_dotted(body.input or {}, "schedule_key") or "").strip()
        return pregen_idempotency_key(capability=spec.name, schedule_key=schedule_key), "pregen_schedule_key"
    key = (body.idempotency_key or "").strip()
    if len(key) < 8:
        raise ValidationFailed(
            "idempotency_key 至少 8 位（重放必须返回原 Run）",
            details={"capability": spec.name},
        )
    return key, "caller"


@router.post("", status_code=202)
async def create_run(body: CreateRunBody, request: Request, operator: OperatorContext = Depends(require_operator)) -> dict[str, Any]:
    spec = get_capability(body.capability)
    if spec is None:
        raise CapabilityUnknown(f"未知能力: {body.capability}")
    _validate_capability_input(spec, body)
    idempotency_key, idempotency_source = _resolve_idempotency_key(spec, body)

    conversation_id = body.conversation_id
    if conversation_id:
        conversation = await conversations_repo.get_conversation(
            request.app.state.pool, conversation_id
        )
        if conversation is None:
            raise NotFound("会话不存在")
        if conversation["crm_user_id"] != operator.user_id:
            raise Forbidden("无权访问该会话")
        thread_id = conversation["thread_id"]
    else:
        thread_id = None  # create_run 内默认 run:{run_id}

    settings = request.app.state.settings
    run, created = await runs_repo.create_run(
        request.app.state.pool,
        idempotency_key=idempotency_key,
        capability=body.capability,
        input_payload=body.input,
        operator={"user_id": operator.user_id, "user_name": operator.user_name},
        thread_id=thread_id,
        conversation_id=conversation_id,
        graph_version=spec.graph_version,
        prompt_version=spec.prompt_version,
        max_attempts=body.max_attempts or settings.worker.max_attempts,
    )
    payload = _serialize(run)
    payload["idempotent_replay"] = not created
    payload["idempotency_source"] = idempotency_source
    payload["read_only"] = spec.read_only
    payload["pregen"] = spec.pregen
    return payload


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request, operator: OperatorContext = Depends(require_operator)) -> dict[str, Any]:
    run = await runs_repo.get_run(request.app.state.pool, run_id)
    return _serialize(_require_owned_run(run, operator))


@router.get("/{run_id}/pending")
async def get_run_pending(run_id: str, request: Request, operator: OperatorContext = Depends(require_operator)) -> dict[str, Any]:
    """Run 级补参信息：等待中的追问内容与状态版本（供补参界面渲染）。"""
    run = await runs_repo.get_run(request.app.state.pool, run_id)
    _require_owned_run(run, operator)
    if run["status"] != "waiting_input":
        return {"waiting": False, "run_id": run_id}
    graph = request.app.state.graphs.get(run["capability"])
    payload = await get_pending_interrupt(graph, run["thread_id"]) if graph else None
    return {
        "waiting": True,
        "run_id": run_id,
        "questions": (payload or {}).get("questions", []),
        "state_version": (payload or {}).get("state_version"),
    }


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request, operator: OperatorContext = Depends(require_operator)) -> dict[str, Any]:
    run = await runs_repo.get_run(request.app.state.pool, run_id)
    _require_owned_run(run, operator)
    cancelled = await runs_repo.cancel_run(request.app.state.pool, run_id=run_id)
    if not cancelled:
        raise RunNotResumable("Run 已结束，不能取消")
    return {"run_id": run_id, "status": "cancelled", "cancelled_at": utcnow().isoformat()}


@router.post("/{run_id}/resume", status_code=202)
async def resume_run(run_id: str, body: ResumeBody, request: Request, operator: OperatorContext = Depends(require_operator)) -> dict[str, Any]:
    run = await runs_repo.get_run(request.app.state.pool, run_id)
    _require_owned_run(run, operator)
    if run["status"] != "waiting_input":
        raise RunNotResumable(f"当前状态 {run['status']} 不可恢复")

    graph = request.app.state.graphs.get(run["capability"])
    if graph is None:
        raise RunNotResumable("能力图未注册")
    interrupt_payload = await get_pending_interrupt(graph, run["thread_id"])
    if interrupt_payload is None:
        # checkpoint 已无等待状态（可能已被恢复过）：返回当前状态，重复 resume 幂等拒绝
        raise RunNotResumable("当前没有等待中的补参请求（可能已被其他请求恢复）")
    expected = int(interrupt_payload.get("state_version", -1))
    if body.state_version != expected:
        raise StateVersionConflict(
            "状态版本不匹配（可能来自旧页面或并发会话）",
            details={"expected": expected, "received": body.state_version},
        )

    accepted = await runs_repo.queue_resume(request.app.state.pool, run_id=run_id, values=body.values)
    if not accepted:
        raise RunNotResumable("恢复请求已被处理")
    return {"run_id": run_id, "status": "queued", "resumed_from": "waiting_input"}
