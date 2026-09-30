"""问答族只读图（analysis-qa@1）：需求 4.6～4.9。

覆盖能力：object.qa（对象内追问）、business.qa（轻量跨对象问答）、
knowledge.qa（知识问答）、meeting.prepare（会前准备）。

硬约束（与需求一致，不做任何放宽）：

- 只读：不创建、不修改任何正式业务字段，不发通知、不自动建会议/派任务；
- 知识授权过滤发生在内容进入模型**之前**：executor 只把 `search_authorized`
  已按 user/public 过滤过的知识注入 `knowledge_facts`，本图不做二次授权，
  也绝不允许先取无权全文再隐藏引用；
- 会话历史只作上下文（`[H]` 行），**不进引用白名单**：模型若引用历史行会被剥离
  并记入 `citation_violations`（历史不是事实来源）；
- 事实/知识不足时把缺口写进 `missing`，不猜测、不生成 SQL、不重算指标
  （指标由 CRM 白名单查询计算）；
- 无任何可用依据（facts 与 knowledge 皆空）时**不调用模型**，直接返回缺失结果；
- 会前准备的 question 由 `meeting.topic` 派生，AI 不自造议题。
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.capabilities import CapabilitySpec, get_capability
from app.graphs.analysis import serialize_facts
from app.graphs.fact_guard import find_unconfirmed_fact
from app.providers.stub import strip_usage_metadata

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_HISTORY_LINE_LIMIT = 500
# 历史预算：长会话若整段入模，会把事实/知识挤掉，也放大成本。
# 只保留最近若干条，且总字符数封顶（保留 recent 优先于保留完整）。
_HISTORY_MAX_ITEMS = 20
_HISTORY_MAX_CHARS = 6000


class QAGraphState(TypedDict, total=False):
    """问答图状态（不保存模型原始返回，只存解析后的结构化结果）。"""

    capability: str
    input_payload: dict[str, Any] | None
    # 问题：入参 question，或会前准备由 meeting.topic 派生
    question: str
    # executor 按操作者授权检索后注入的知识（已过滤，先于内容进模型）
    knowledge_facts: list[dict[str, Any]]
    # 会话历史（[H] 行），仅作上下文，不进引用白名单
    history: list[dict[str, Any]]
    history_text: str
    facts_text: str
    user_text: str
    fact_refs: list[dict[str, str]]
    knowledge_refs: list[dict[str, str]]
    # 是否有可依据的事实/知识：False 时不调用模型
    has_basis: bool
    analysis: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None


def _dotted(payload: dict[str, Any], path: str) -> Any:
    """读取 input 里的点路径值（如 window.from / meeting.topic）；缺失返回 None。"""
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _has_text(fact: dict[str, Any]) -> bool:
    return any(
        isinstance(fact.get(key), str) and str(fact.get(key)).strip()
        for key in ("text", "summary", "content")
    )


def _validate_fact_items(facts: list[Any], *, label: str) -> str | None:
    """校验事实/知识行：必须是对象、带稳定 id 与可读文本（引用完整性需要）。

    另按 M-01 拦截未确认候选：候选不计入正式事实，不能参与只读问答与归纳。
    """
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            return f"{label}[{index}] 必须是对象"
        if not str(fact.get("id", "")).strip():
            return f"{label}[{index}] 缺少稳定 id（引用完整性需要）"
        if not _has_text(fact):
            return f"{label}[{index}] 缺少可读文本（text/summary/content 至少一个）"
    problem = find_unconfirmed_fact(facts, label=label)
    if problem:
        return f"{problem}（候选只能待本人确认后由 CRM 写入正式事实）"
    return None


def serialize_history(history: list[dict[str, Any]]) -> str:
    """会话历史 → `[H]` 行。历史只是上下文，不产生引用（不可作为 citations 来源）。

    预算控制：只取**最近**的若干条并封顶总字符数——历史是"最近的语境"，
    越旧参考价值越低；若不封顶，长会话会把事实与知识挤出上下文窗口。
    返回行仍从 1 重新编号，编号只用于上下文，不是引用 id。
    """
    prepared: list[tuple[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        role = str(item.get("role") or "user").strip()
        prepared.append((role, content[:_HISTORY_LINE_LIMIT]))

    kept: list[tuple[str, str]] = []
    used = 0
    for role, content in reversed(prepared):
        if len(kept) >= _HISTORY_MAX_ITEMS:
            break
        if used + len(content) > _HISTORY_MAX_CHARS and kept:
            break
        kept.append((role, content))
        used += len(content)
    kept.reverse()

    return "\n".join(
        f"[H{index}] role={role} text={content}"
        for index, (role, content) in enumerate(kept, start=1)
    )


def _resolve_question(payload: dict[str, Any]) -> str:
    """问题：优先入参 question；会前准备由 meeting.topic 派生（AI 不自造议题）。"""
    question = str(payload.get("question") or "").strip()
    if question:
        return question
    topic = _dotted(payload, "meeting.topic")
    if isinstance(topic, str) and topic.strip():
        return (
            f"为会议《{topic.strip()}》做会前准备：结合已给出的正式沟通/待办事实与有权知识，"
            "列出背景要点、待确认事项与建议（只做只读准备，不建会议、不对外发送）。"
        )
    return ""


def guard_qa_node(state: QAGraphState) -> dict[str, Any]:
    """守卫：能力登记、必填输入、条数上限、授权事实/知识完整性。不满足即失败（不调模型）。"""
    capability = str(state.get("capability") or "")
    spec = get_capability(capability)
    if spec is None:
        return {"error": f"未知能力: {capability}"}
    if spec.kind != "analysis" or not spec.read_only:
        return {"error": f"能力 {capability} 不是只读问答能力，不能走问答图"}

    payload = state.get("input_payload") or {}
    if not isinstance(payload, dict):
        return {"error": "input_payload 必须是对象"}

    missing = [path for path in spec.required_input_fields if not _dotted(payload, path)]
    if missing:
        return {"error": f"缺少必填输入: {', '.join(missing)}"}

    question = _resolve_question(payload)
    if not question:
        return {"error": "缺少 question（会前准备可由 meeting.topic 派生）"}

    # facts：必填能力为空即失败；可选能力（knowledge.qa）允许为空
    raw_facts = payload.get("facts")
    facts: list[dict[str, Any]] = []
    if isinstance(raw_facts, list) and raw_facts:
        if spec.max_facts is not None and len(raw_facts) > spec.max_facts:
            return {
                "error": (
                    f"input.facts 超过 {spec.max_facts} 条上限"
                    "（跨对象问答只调用白名单查询，限制条数与时间窗，指标由 CRM 计算）"
                )
            }
        problem = _validate_fact_items(raw_facts, label="facts")
        if problem:
            return {"error": problem}
        facts = [item for item in raw_facts if isinstance(item, dict)]
    elif spec.requires_facts:
        return {"error": "facts 不能为空（必须由 CRM 按权限装配后随 Run 传入）"}

    # knowledge：executor 已按操作者授权检索过滤后注入
    raw_knowledge = state.get("knowledge_facts") or []
    knowledge: list[dict[str, Any]] = []
    if isinstance(raw_knowledge, list) and raw_knowledge:
        problem = _validate_fact_items(raw_knowledge, label="knowledge_facts")
        if problem:
            return {"error": problem}
        knowledge = [item for item in raw_knowledge if isinstance(item, dict)]

    # 事实在前、知识在后，统一编号避免 F1 重复；引用白名单只含这两类
    combined = facts + knowledge
    combined_text, combined_refs = serialize_facts(combined) if combined else ("", [])
    fact_refs = combined_refs[: len(facts)]
    knowledge_refs = combined_refs[len(facts) :]

    history = state.get("history") or []
    history_text = serialize_history(history) if isinstance(history, list) else ""

    parts = [part for part in (combined_text, history_text) if part]
    return {
        "question": question,
        "facts_text": combined_text,
        "fact_refs": fact_refs,
        "knowledge_refs": knowledge_refs,
        "history_text": history_text,
        "user_text": "\n".join(parts),
        "has_basis": bool(combined),
    }


async def analyze_qa_node(state: QAGraphState, *, model: BaseChatModel) -> dict[str, Any]:
    """调用模型回答；输出不是合法 JSON 视为节点失败（不静默造假结果）。"""
    capability = str(state.get("capability") or "")
    spec = get_capability(capability)
    if spec is None or not spec.prompt_template:
        return {"error": f"能力 {capability} 未登记 Prompt 模板"}
    prompt = spec.prompt_template.format(
        capability=spec.name,
        description=spec.description,
        question=state.get("question") or "",
        user_text=state.get("user_text") or "",
    )
    response = await model.ainvoke(prompt)
    content = response.content if isinstance(response.content, str) else str(response.content)
    match = _JSON_RE.search(content)
    if not match:
        return {"error": "模型输出不是合法 JSON 问答结果"}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"error": "模型输出 JSON 解析失败"}
    if not isinstance(payload, dict):
        return {"error": "模型输出必须是 JSON 对象"}
    return {"analysis": strip_usage_metadata(payload)}


def _points(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _empty_result(state: QAGraphState, spec: CapabilitySpec | None) -> dict[str, Any]:
    """无可用依据时的结果：不调用模型，显式披露缺口（不猜测、不编造）。"""
    capability = str(state.get("capability") or "")
    return {
        "capability": capability,
        "kind": "qa",
        "read_only": True,
        "summary": "",
        "sections": [],
        "missing": ["无可用事实或知识依据（未调用模型，不猜测、不编造）"],
        "suggestions": [],
        "citations": [],
        "citation_violations": [],
        "facts_used": {"count": 0, "refs": []},
        "knowledge_used": {"count": 0, "refs": []},
        "notes": (
            "只读结果：无依据时不调用模型；不写入任何正式业务对象；"
            f"{spec.description if spec else ''}"
        ).strip(),
    }


def assemble_empty_node(state: QAGraphState) -> dict[str, Any]:
    return {"result": _empty_result(state, get_capability(str(state.get("capability") or "")))}


def assemble_qa_node(state: QAGraphState) -> dict[str, Any]:
    """装配只读问答结果：引用越界剥离并记录，输出不含任何写入指令。"""
    capability = str(state.get("capability") or "")
    spec = get_capability(capability)
    analysis = state.get("analysis") or {}
    fact_refs = state.get("fact_refs") or []
    knowledge_refs = state.get("knowledge_refs") or []
    refs = fact_refs + knowledge_refs
    # 引用白名单只含事实与知识行；历史 [H] 行永远不可引用
    allowed = {ref["ref_id"] for ref in refs}

    sections: list[dict[str, Any]] = []
    for section in analysis.get("sections") or []:
        if not isinstance(section, dict):
            continue
        points = _points(section.get("points"))
        if not points:
            continue
        sections.append({"title": str(section.get("title", "")).strip(), "points": points})

    citations: list[dict[str, str]] = []
    violations: list[str] = []
    for citation in analysis.get("citations") or []:
        if isinstance(citation, dict):
            ref_id = str(citation.get("ref_id", "")).strip()
            version = str(citation.get("version", "")).strip()
            ctype = str(citation.get("type", "")).strip()
        elif isinstance(citation, str):
            ref_id, version, ctype = citation.strip(), "", ""
        else:
            continue
        if not ref_id:
            continue
        if ref_id not in allowed:
            # 编造引用或引用历史：一律剥离并记录，绝不放行
            violations.append(ref_id)
            continue
        matched = next(ref for ref in refs if ref["ref_id"] == ref_id)
        citations.append(
            {
                "ref_id": ref_id,
                "type": ctype or matched["type"],
                "version": version or matched["version"],
            }
        )

    summary = str(analysis.get("summary", "")).strip()
    if not summary and not sections:
        return {"error": "模型输出缺少 summary 与 sections，无法形成只读结果"}

    result = {
        "capability": capability,
        "kind": "qa",
        "read_only": True,
        "summary": summary,
        "sections": sections,
        "missing": _points(analysis.get("missing")),
        "suggestions": _points(analysis.get("suggestions")),
        "citations": citations,
        "citation_violations": violations,
        "facts_used": {"count": len(fact_refs), "refs": fact_refs},
        "knowledge_used": {"count": len(knowledge_refs), "refs": knowledge_refs},
        "notes": (
            "只读结果：不写入任何正式业务对象、不派任务、不发通知；"
            f"{spec.description if spec else ''}"
        ).strip(),
    }
    return {"result": strip_usage_metadata(result)}


def route_after_guard(state: QAGraphState) -> str:
    """无依据直接装配缺失结果（不调模型）；有依据才走模型。"""
    if state.get("error"):
        return END
    return "analyze" if state.get("has_basis") else "assemble_empty"


def route_after_analyze(state: QAGraphState) -> str:
    return END if state.get("error") else "assemble"


def build_qa_graph(spec: CapabilitySpec, model: BaseChatModel) -> StateGraph:
    graph = StateGraph(QAGraphState)

    async def _analyze(state: QAGraphState) -> dict[str, Any]:
        return await analyze_qa_node(state, model=model)

    graph.add_node("guard", guard_qa_node)
    graph.add_node("analyze", _analyze)
    graph.add_node("assemble", assemble_qa_node)
    graph.add_node("assemble_empty", assemble_empty_node)

    graph.add_edge(START, "guard")
    graph.add_conditional_edges("guard", route_after_guard)
    graph.add_conditional_edges("analyze", route_after_analyze)
    graph.add_edge("assemble", END)
    graph.add_edge("assemble_empty", END)
    return graph


def compile_qa_graph(
    spec: CapabilitySpec, model: BaseChatModel, checkpointer: Any | None = None
) -> CompiledStateGraph:
    return build_qa_graph(spec, model).compile(checkpointer=checkpointer)
