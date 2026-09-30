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

# 图版本常量（需求 5.1：图版本与能力绑定；注册表是唯一真源）
ANALYSIS_GRAPH_VERSION = "analysis@1"
# 问答族（需求 4.6～4.9）走独立问答图：历史只作上下文、知识先过滤后入模
QA_GRAPH_VERSION = "analysis-qa@1"

# 问答类能力（需求 4.6～4.9）的统一输出契约：同一 JSON 结构，多一路会话历史与知识行。
QA_PROMPT_TEMPLATE = """你是 CRM 销售助理，只做只读问答与准备，不创建、不修改任何正式业务对象，也不发通知。
能力：{capability}（{description}）
问题：{question}

要求：
- 只依据 <<USER_TEXT>> 中给出的行：`[F]` 行是事实与知识（已按你的权限过滤），`[H]` 行是会话历史；
- 事实与推断分开：结论必须落到具体行；推断要在结果里明确标注为推断；
- 事实或知识不足时把缺口写进 missing，不猜测、不生成 SQL、不重算指标（指标由 CRM 白名单查询计算）；
- citations 只能引用 `[F]` 行里出现的 id 与 version，不得引用历史行，也不得编造；
- 只输出 JSON，不要输出任何其他文字。

JSON 结构：
{{
  "summary": "对问题的回答（只读）",
  "sections": [{{"title": "小节标题", "points": ["要点"]}}],
  "missing": ["缺失的事实或知识说明"],
  "suggestions": ["下一步建议（只读，不派任务不发通知）"],
  "citations": [{{"ref_id": "[F] 行中的 id", "type": "事实或知识类型", "version": "版本"}}]
}}

<<OUTPUT_CONTRACT>>analysis<<END_OUTPUT_CONTRACT>>
<<CAPABILITY>>{capability}<<END_CAPABILITY>>
<<USER_TEXT>>
{user_text}
<<END_USER_TEXT>>"""
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
    # 必须在会话中发起（对象内追问要求 conversation_id）
    requires_conversation: bool = False
    # 会话必须绑定到 input.scope 指定的对象（对象绑定会话）
    requires_subject_binding: bool = False
    # 执行时由 AI 按操作者授权检索知识索引（过滤先于内容进模型）
    retrieves_knowledge: bool = False
    # 执行时加载会话历史（对象内追问用有效历史）
    requires_history: bool = False
    # facts 条数上限（跨对象问答限制条数；时间窗由 input.window 约束）
    max_facts: int | None = None
    # 入口可见条件（移动端 PRD 对齐 M-12）：前端据此决定入口是否展示，
    # 不由前端自行推断能力可用性（避免"页面有按钮但调用必失败"）。
    entry_conditions: tuple[str, ...] = ()
    # 源对象返回路径：结果回跳到哪个页面/对象（M-12 要求入口声明返回路径）
    return_path: str = ""


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
        entry_conditions=('需用户主动提交沟通文本/录音转写结果', '不得由系统自动采集触发（M-04）'),
        return_path='communication:detail',
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
        entry_conditions=('仅当日窗口', '受益人本人可见（预生成由 CRM 调度）'),
        return_path='daily:detail',
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
        entry_conditions=('需在客户/线索/商机详情页', '操作者对该对象有查看权'),
        return_path='object:detail:{scope.subject_type}:{scope.subject_id}',
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
        entry_conditions=('本人视角', '数据来自 CRM 确定性底单（不恢复模型排序）'),
        return_path='today:list',
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
        entry_conditions=('操作者具团队查看权限', '只读团队底单，不评分不派任务'),
        return_path='manager:dashboard',
    ),
    "object.qa": CapabilitySpec(
        name="object.qa",
        kind="analysis",
        graph_version=QA_GRAPH_VERSION,
        prompt_version="object-qa-prompt@1",
        description="对象内追问：对象绑定会话中用有效历史与当前事实回答，事实与推断分开",
        read_only=True,
        pregen=False,
        requires_text=False,
        requires_facts=True,
        required_input_fields=("question", "facts", "scope.subject_type", "scope.subject_id"),
        prompt_template=QA_PROMPT_TEMPLATE,
        requires_conversation=True,
        requires_subject_binding=True,
        requires_history=True,
        entry_conditions=('对象详情页内', '已建立对象绑定会话 conversation_id'),
        return_path='object:detail:{scope.subject_type}:{scope.subject_id}',
    ),
    "business.qa": CapabilitySpec(
        name="business.qa",
        kind="analysis",
        graph_version=QA_GRAPH_VERSION,
        prompt_version="business-qa-prompt@1",
        description="轻量跨对象问答：只用 CRM 白名单查询结果（限制条数与时间窗），指标由 CRM 计算",
        read_only=True,
        pregen=False,
        requires_text=False,
        requires_facts=True,
        required_input_fields=("question", "facts", "window.from", "window.to"),
        prompt_template=QA_PROMPT_TEMPLATE,
        max_facts=50,
        entry_conditions=('跨对象问答入口', '事实条数与时间窗受契约限制'),
        return_path='business:qa',
    ),
    "knowledge.qa": CapabilitySpec(
        name="knowledge.qa",
        kind="analysis",
        graph_version=QA_GRAPH_VERSION,
        prompt_version="knowledge-qa-prompt@1",
        description="知识问答：检索已发布且本人有权的知识版本，返回引用与版本；过滤先于内容进模型",
        read_only=True,
        pregen=False,
        requires_text=False,
        requires_facts=False,  # 知识由 AI 索引按授权检索，facts 可选（CRM 可附上下文事实）
        required_input_fields=("question",),
        prompt_template=QA_PROMPT_TEMPLATE,
        retrieves_knowledge=True,
        entry_conditions=('存在已发布且本人有权的知识', '撤权后立即从可用集合消失'),
        return_path='knowledge:qa',
    ),
    "meeting.prepare": CapabilitySpec(
        name="meeting.prepare",
        kind="analysis",
        graph_version=QA_GRAPH_VERSION,
        prompt_version="meeting-prepare-prompt@1",
        description="会前准备：组合正式沟通/待办与有权知识生成准备要点；不自动建会议、不对外发送",
        read_only=True,
        pregen=False,
        requires_text=False,
        requires_facts=True,
        required_input_fields=("facts", "meeting.topic"),
        prompt_template=QA_PROMPT_TEMPLATE,
        retrieves_knowledge=True,
        entry_conditions=('会议上下文中（topic 必填）', '只读准备，不自动建会议/不对外发送'),
        return_path='meeting:prepare',
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