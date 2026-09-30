"""事实入模守卫：未确认候选不得当作事实（移动端 PRD 对齐 M-01）。

M-01 明确：候选是"AI 待确认"，**不计入正式事实**；允许持久化用于恢复/审计，
但不能作为事实参与任何只读归纳或问答。日报底稿尤其如此——需求 4.4 与 M-09
都要求底稿只含正式事实，不含未确认候选。

本守卫是纵深防御：事实由 CRM 装配，理论上不应混入候选；但若装配侧出Bug或
后续有人图省事直接把候选塞进 facts，这里会直接失败而不是静默把候选当事实
送进模型（否则会出现"AI 用自己未确认的猜测给自己作证"）。
"""
from __future__ import annotations

from typing import Any

# 明确表示"候选/未确认"的类型标记
_CANDIDATE_TYPES = frozenset(
    {"candidate", "ai_candidate", "draft", "ai_draft", "suggestion", "ai_suggestion"}
)
# 明确表示未确认的状态值
_UNCONFIRMED_STATUS = frozenset(
    {"pending", "unconfirmed", "draft", "待确认", "未确认", "待处理"}
)


def unconfirmed_reason(fact: dict[str, Any]) -> str | None:
    """若该行是未确认候选，返回原因；可作为正式事实则返回 None。"""
    if not isinstance(fact, dict):
        return "不是对象"

    fact_type = str(fact.get("type") or "").strip().lower()
    if fact_type in _CANDIDATE_TYPES:
        return f"type={fact_type} 属于未确认候选，不能作为正式事实入模"

    # 显式布尔标记：confirmed=false 一律拒绝（不管类型叫什么）
    if fact.get("confirmed") is False:
        return "confirmed=false 的候选不能作为正式事实入模"

    status = str(fact.get("status") or "").strip().lower()
    if status in _UNCONFIRMED_STATUS:
        return f"status={status} 属于未确认候选，不能作为正式事实入模"

    return None


def find_unconfirmed_fact(
    facts: list[Any], *, label: str = "facts"
) -> str | None:
    """校验一批事实行；返回第一个违规说明，全部合规返回 None。"""
    for index, fact in enumerate(facts):
        reason = unconfirmed_reason(fact)
        if reason:
            fact_id = ""
            if isinstance(fact, dict):
                fact_id = str(fact.get("id") or "").strip()
            suffix = f"（id={fact_id}）" if fact_id else ""
            return f"{label}[{index}]{suffix} {reason}"
    return None
