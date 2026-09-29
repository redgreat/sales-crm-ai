"""只读分析类能力的通用图：事实守卫 → 模型归纳 → 结果装配。

用于 daily.draft / object.summary / today.summary / manager.focus
（需求 4.4 日报、4.5 对象摘要、4.10 今日任务、4.11 主管关注）。

硬约束：
- facts 由 CRM 按权限装配后随 Run 传入，AI 不查业务库、不扩大范围、不做二次授权；
- 只读：不写任何正式业务对象；日报工作稿与提交快照留在 CRM；
- 事实缺失 / 引用越界时显式失败或剥离，不编造结果；
- 预生成能力必须带 beneficiary 与 schedule_key，AI 不自行指定受益人。

事实行约定（与 Stub 共用同一格式，真实模型走同一 Prompt/schema）：
    [F1] type=activity id=ACT-1 version=3 occurred_at=2026-09-29 text=客户拜访，讨论续约
citations 中的 ref_id 必须命中这些 id，否则进入 citation_violations。
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.capabilities import CapabilitySpec, get_capability
from app.providers.stub import strip_usage_metadata

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_FACT_LINE_LIMIT = 200


class AnalysisGraphState(TypedDict, total=False):
    """只读分析图状态（不保存模型原始返回，只存解析后的结构化结果）。"""

    capability: str
    input_payload: dict[str, Any] | None
    # 问答类（4.6～4.9）：问题、按授权检索到的知识事实、会话历史（executor 执行前注入）
    question: str
    knowledge_facts: list[dict[str, Any]]
    history: list[dict[str, Any]]
    history_text: str
    facts_text: str
    fact_refs: list[dict[str, str]]
    analysis: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None


def _fact_text(fact: dict[str, Any]) -> str:
    for key in ("text", "summary", "content"):
        value = fact.get(key)
        if isinstance(value, str) and value.strip():
            # 知识快照需要更多上下文；普通 CRM 事实保持短行
            limit = 1200 if str(fact.get("type") or "") == "knowledge" else _FACT_LINE_LIMIT
            return value.strip().replace("\n", " ")[:limit]
    return ""


def serialize_facts(facts: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """事实 → 模型可见文本 + 引用白名单（id/type/version）。

    只序列化 CRM 已授权的事实；其他字段不进 Prompt（防止越权材料入模）。
    """
    lines: list[str] = []
    refs: list[dict[str, str]] = []
    for index, fact in enumerate(facts, start=1):
        ref = f"F{index}"
        fact_id = str(fact.get("id", "")).strip()
        fact_type = str(fact.get("type", "other")).strip() or "other"
        version = str(fact.get("version", "")).strip()
        occurred_at = str(fact.get("occurred_at", "")).strip()
        parts = [f"[{ref}]", f"type={fact_type}", f"id={fact_id}"]
        if version:
            parts.append(f"version={version}")
        if occurred_at:
            parts.append(f"occurred_at={occurred_at}")
        parts.append(f"text={_fact_text(fact)}")
        lines.append(" ".join(parts))
        refs.append({"ref": ref, "ref_id": fact_id, "type": fact_type, "version": version})
    return "\n".join(lines), refs


def _validate_facts(payload: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    facts = payload.get("facts")
    if not isinstance(facts, list) or not facts:
        return None, "facts 不能为空（必须由 CRM 按权限装配后随 Run 传入）"
    normalized: list[dict[str, Any]] = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            return None, f"facts[{index}] 必须是对象"
        if not str(fact.get("id", "")).strip():
            return None, f"facts[{index}] 缺少稳定 id（引用完整性需要）"
        if not _fact_text(fact):
            return None, f"facts[{index}] 缺少可读文本（text/summary/content 至少一个）"
        normalized.append(fact)
    return normalized, None


def _dotted(payload: dict[str, Any], path: str) -> Any:
    """读取 input 里的点路径值（如 window.from）；缺失返回 None。"""
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def guard_facts_node(state: AnalysisGraphState) -> dict[str, Any]:
    """事实守卫：能力登记、必填字段、授权事实完整性。任一不满足即失败（不调模型）。"""
    capability = str(state.get("capability") or "")
    spec = get_capability(capability)
    if spec is None:
        return {"error": f"未知能力: {capability}"}
    if not spec.read_only:
        return {"error": f"能力 {capability} 不是只读分析能力，不能走分析图"}

    payload = state.get("input_payload") or {}
    if not isinstance(payload, dict):
        return {"error": "input_payload 必须是对象"}

    missing = [path for path in spec.required_input_fields if not _dotted(payload, path)]
    if missing:
        return {"error": f"缺少必填输入: {', '.join(missing)}"}
    if spec.pregen:
        if not _dotted(payload, "beneficiary.user_id"):
            return {"error": "预生成能力必须显式指定受益人 beneficiary.user_id"}
        if not _dotted(payload, "schedule_key"):
            return {"error": "预生成能力必须显式指定 schedule_key（调度幂等）"}

    facts, problem = _validate_facts(payload)
    if problem or facts is None:
        return {"error": problem or "facts 校验失败"}

    facts_text, refs = serialize_facts(facts)
    return {"facts_text": facts_text, "fact_refs": refs}


async def analyze_node(state: AnalysisGraphState, *, model: BaseChatModel) -> dict[str, Any]:
    """调用模型归纳；输出不是合法 JSON 视为节点失败（不静默造假结果）。"""
    capability = str(state.get("capability") or "")
    spec = get_capability(capability)
    if spec is None or not spec.prompt_template:
        return {"error": f"能力 {capability} 未登记 Prompt 模板"}
    prompt = spec.prompt_template.format(
        capability=spec.name, description=spec.description, user_text=state.get("facts_text") or ""
    )
    response = await model.ainvoke(prompt)
    content = response.content if isinstance(response.content, str) else str(response.content)
    match = _JSON_RE.search(content)
    if not match:
        return {"error": "模型输出不是合法 JSON 分析结果"}
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


def assemble_node(state: AnalysisGraphState) -> dict[str, Any]:
    """装配只读结果：引用越界剥离并记录，输出不含任何写入指令。"""
    capability = str(state.get("capability") or "")
    spec = get_capability(capability)
    analysis = state.get("analysis") or {}
    refs = state.get("fact_refs") or []
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
            # 模型编造引用：剥离并记录，绝不放行（事实与推断分开、引用可核）
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
        "kind": "analysis",
        "read_only": True,
        "summary": summary,
        "sections": sections,
        "missing": _points(analysis.get("missing")),
        "suggestions": _points(analysis.get("suggestions")),
        "citations": citations,
        "citation_violations": violations,
        "facts_used": {"count": len(refs), "refs": refs},
        "notes": f"只读结果：不写入任何正式业务对象；{spec.description if spec else ''}".strip(),
    }
    return {"result": strip_usage_metadata(result)}


def route_after_guard(state: AnalysisGraphState) -> str:
    return END if state.get("error") else "analyze"


def route_after_analyze(state: AnalysisGraphState) -> str:
    return END if state.get("error") else "assemble"


def build_analysis_graph(spec: CapabilitySpec, model: BaseChatModel) -> StateGraph:
    graph = StateGraph(AnalysisGraphState)

    async def _analyze(state: AnalysisGraphState) -> dict[str, Any]:
        return await analyze_node(state, model=model)

    graph.add_node("guard_facts", guard_facts_node)
    graph.add_node("analyze", _analyze)
    graph.add_node("assemble", assemble_node)

    graph.add_edge(START, "guard_facts")
    graph.add_conditional_edges("guard_facts", route_after_guard)
    graph.add_conditional_edges("analyze", route_after_analyze)
    graph.add_edge("assemble", END)
    return graph


def compile_analysis_graph(
    spec: CapabilitySpec, model: BaseChatModel, checkpointer: Any | None = None
) -> CompiledStateGraph:
    return build_analysis_graph(spec, model).compile(checkpointer=checkpointer)