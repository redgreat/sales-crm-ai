"""能力注册表：能力名 → 图版本/Prompt 版本/输入契约/只读与预生成属性。

来源：需求 4（全量业务能力）与 5.1（记录图版本、Schema、Prompt 版本）。
新增能力必须先在此登记：API 对未登记能力直接返回 CAPABILITY_UNKNOWN，
不做隐式兜底，也不按功能重建 Provider（各能力共用创建 Run/查询/取消契约）。

两类能力：
- extract：抽取型（communication.extract），产出候选建议，由 CRM 侧确认后正式写入；
- analysis：只读分析型（日报/对象摘要/今日任务/主管关注），只吃 CRM 装配的
  有权事实，绝不写正式字段，日报工作稿与提交快照留在 CRM。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CapabilityKind = Literal["extract", "analysis"]

# 只读分析类能力的统一输出契约（模型必须按此 JSON 结构输出）
ANALYSIS_PROMPT_TEMPLATE = """你是 CRM 销售助理，只做只读归纳，不创建、不修改任何正式业务对象。
能力：{capability}（{description}）

要求：
- 只依据 <<USER_TEXT>> 中给出的事实行，不引入外部信息，不编造事实；
- 事实不足时把缺口写进 missing，不要猜测，也不要用通用建议填空；
- citations 只能引用事实行里出现的 id 与 version，不得编造引用；
- 只输出 JSON，不要输出任何其他文字。

JSON 结构：
{{
  "summary": "一段归纳（只描述事实，不给绩效结论）",
  "sections": [{{"title": "小节标题", "points": ["要点"]}}],
  "missing": ["缺失的事实说明"],
  "suggestions": ["下一步建议（只读建议，不派任务）"],
  "citations": [{{"ref_id": "事实行中的 id", "type": "事实类型", "version": "事实版本"}}]
}}

<<OUTPUT_CONTRACT>>analysis<<END_OUTPUT_CONTRACT>>
<<CAPABILITY>>{capability}<<END_CAPABILITY>>
<<USER_TEXT>>
{user_text}
<<END_USER_TEXT>>"""


@dataclass(frozen=True)
class CapabilitySpec:
    """单个能力的契约声明。"""

    name: str
    kind: CapabilityKind
    graph_version: str
    prompt_version: str
    description: str
    # 只读：不写任何正式业务对象（analysis 恒为 True）
    read_only: bool = True
    # 允许 CRM 按受益人预生成（日报/今日任务/主管关注）；调度幂等由 schedule_key 派生
    pregen: bool = False
    # 是否需要 input.text（抽取型需要；分析型用 facts）
    requires_text: bool = False
    # 是否要求 CRM 装配有权事实
    requires_facts: bool = True
    # 必填的 input 点路径（如 "window.from"）
    required_input_fields: tuple[str, ...] = ()
    prompt_template: str = ""


CAPABILITIES: dict[str, CapabilitySpec] = {
    "communication.extract": CapabilitySpec(
        name="communication.extract",
        kind="extract",
        graph_version="extract@1",
        prompt_version="extract-prompt@1",
        description="沟通整理：抽取活动与任务候选建议，由本人确认后写入 CRM",
        read_only=True,
        pregen=False,
        requires_text=True,
        requires_facts=False,
        required_input_fields=("text",),
    ),
    "daily.draft": CapabilitySpec(
        name="daily.draft",
        kind="analysis",
        graph_version="analysis@1",
        prompt_version="daily-draft-prompt@1",
        description="日报底稿：读取当日正式事实生成底稿，不含未确认候选；工作稿与提交快照留在 CRM",
        read_only=True,
        pregen=True,
        requires_text=False,
        requires_facts=True,
        required_input_fields=("facts", "window.from", "window.to"),
        prompt_template=ANALYSIS_PROMPT_TEMPLATE,
    ),
    "object.summary": CapabilitySpec(
        name="object.summary",
        kind="analysis",
        graph_version="analysis@1",
        prompt_version="object-summary-prompt@1",
        description="客户/线索/商机摘要：只读状态、事实、缺失与建议，不覆盖正式字段",
        read_only=True,
        pregen=False,
        requires_text=False,
        requires_facts=True,
        required_input_fields=("facts", "scope.subject_type", "scope.subject_id"),
        prompt_template=ANALYSIS_PROMPT_TEMPLATE,
    ),
    "today.summary": CapabilitySpec(
        name="today.summary",
        kind="analysis",
        graph_version="analysis@1",
        prompt_version="today-summary-prompt@1",
        description="今日任务总结：归纳 CRM 提供的确定性底单，不创建任务、不另造权威优先级",
        read_only=True,
        pregen=True,
        requires_text=False,
        requires_facts=True,
        required_input_fields=("facts", "window.from", "window.to"),
        prompt_template=ANALYSIS_PROMPT_TEMPLATE,
    ),
    "manager.focus": CapabilitySpec(
        name="manager.focus",
        kind="analysis",
        graph_version="analysis@1",
        prompt_version="manager-focus-prompt@1",
        description="主管关注：只读团队权限内底单，不评分、不作绩效结论、不自动派任务",
        read_only=True,
        pregen=True,
        requires_text=False,
        requires_facts=True,
        required_input_fields=("facts", "window.from", "window.to"),
        prompt_template=ANALYSIS_PROMPT_TEMPLATE,
    ),
}


def get_capability(name: str) -> CapabilitySpec | None:
    return CAPABILITIES.get(name)


def capability_versions() -> dict[str, str]:
    """能力 → 图版本登记（需求 5.1：记录图版本）。"""
    return {name: spec.graph_version for name, spec in CAPABILITIES.items()}


def analysis_capabilities() -> dict[str, CapabilitySpec]:
    return {name: spec for name, spec in CAPABILITIES.items() if spec.kind == "analysis"}


def pregen_capabilities() -> tuple[str, ...]:
    """允许预生成的能力（需求 4 结尾段：仅日报底稿/今日任务/主管关注）。"""
    return tuple(name for name, spec in CAPABILITIES.items() if spec.pregen)


def pregen_idempotency_key(*, capability: str, schedule_key: str) -> str:
    """预生成的调度幂等键：同一 (能力, 受益窗口键) 重复调度返回同一 Run。

    CRM 按受益人/窗口计算 schedule_key，Python 侧据此做持久幂等，
    不在两端重复调度同一业务任务（需求 4 结尾段）。
    """
    return f"pregen:{capability}:{schedule_key}"