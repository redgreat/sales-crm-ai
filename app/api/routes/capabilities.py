"""能力目录：前端入口的唯一真源（移动端 PRD 对齐 M-12）。

M-12 指出移动端"未清楚对应客户/线索/商机摘要、对象问答、会前准备、今日任务
及主管关注"，并要求每个入口给出**可见条件、当前上下文、能力码、只读标识和
源对象返回路径**。

此前前端没有可发现能力的接口，只能硬编码按钮——一旦注册表变更就会漂移成
"页面有按钮但调用必失败"。本端点直接由注册表渲染，能力增删只改一处。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.capabilities import CAPABILITIES, CapabilitySpec

router = APIRouter(prefix="/api/v1/capabilities", tags=["capabilities"])


def _entry(spec: CapabilitySpec) -> dict[str, Any]:
    return {
        # 能力码：前端不得自造能力名，未登记能力服务端返回 CAPABILITY_UNKNOWN
        "capability": spec.name,
        "kind": spec.kind,
        "description": spec.description,
        # 只读标识：true 表示绝不写正式业务对象，前端不应展示"确认写入"类动作
        "read_only": spec.read_only,
        "pregen": spec.pregen,
        "graph_version": spec.graph_version,
        "prompt_version": spec.prompt_version,
        # 当前上下文：调用前必须备齐的入参（点路径）
        "context_required": list(spec.required_input_fields),
        "requires_text": spec.requires_text,
        "requires_facts": spec.requires_facts,
        "requires_conversation": spec.requires_conversation,
        "requires_subject_binding": spec.requires_subject_binding,
        "max_facts": spec.max_facts,
        # 可见条件：由服务端声明，不由前端推断
        "entry_conditions": list(spec.entry_conditions),
        # 源对象返回路径：结果回跳目标
        "return_path": spec.return_path,
    }


@router.get("")
async def list_capabilities() -> dict[str, Any]:
    """返回全部已登记能力的入口契约（注册表是唯一真源）。"""
    items = [_entry(spec) for spec in CAPABILITIES.values()]
    return {
        "capabilities": items,
        "count": len(items),
        "notes": (
            "入口契约由能力注册表渲染；未登记能力服务端一律返回 CAPABILITY_UNKNOWN。"
            "read_only=true 的能力不写任何正式业务对象，前端不得展示确认写入动作。"
        ),
    }


@router.get("/{capability}")
async def get_capability_entry(capability: str) -> dict[str, Any]:
    from app.errors import CapabilityUnknown

    spec = CAPABILITIES.get(capability)
    if spec is None:
        raise CapabilityUnknown(f"未知能力: {capability}")
    return _entry(spec)
