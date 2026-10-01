"""助手建档草稿图（draft@1）：移动端 M20 的 6 项能力中 4 项「新增类」。

覆盖能力：customer.draft / contact.draft / lead.draft / opportunity.draft。

硬约束（与 M20 产品红线一致，不做任何放宽）：

- 确认前不落库：图的产物只是草稿（result.draft），AI 不写任何正式业务表；
  正式写入由 CRM 在本人确认后调用对应建档 Service 完成（先匹配再新增）；
- 创建类草稿必须补齐必填字段才能确认：缺什么追问什么（interrupt，等待不占 worker）；
- 只写本人权限内对象：写入时由 CRM 正式 Service 的数据范围规则判定，
  AI 不理解组织结构、不替人挑负责人；
- 每次写入与查询均标记来源（来源=助手；谁/何时/原始输入留痕由候选与 Run 承载）；
- 恢复时应答仍缺参则用递增后的 state_version 再次 interrupt（防旧 resume 重放）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.graphs.state import ExtractGraphState
from app.providers.stub import strip_usage_metadata

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# 各草稿类型的必填业务字段（缺失即追问；与 CRM 确认写入的 DTO 必填一致）
DRAFT_REQUIRED_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    # (字段, 追问话题)
    "customer": (("name", "客户名称"), ("customer_type", "客户类型（如 ORG 企业客户 / IND 个体客户）")),
    "contact": (("customer_name", "所属客户名称"), ("contact_name", "联系人姓名")),
    "lead": (("raw_content", "线索内容（客户意向/需求描述）"),),
    "opportunity": (("customer_name", "所属客户名称"), ("name", "商机名称")),
}

DRAFT_PROMPT_TEMPLATE = """你是 CRM 销售助理。用户想{intent}，从下面的一句话/图片转写内容里整理出建档草稿，只输出 JSON，不要输出其他文字。

草稿类型: {draft_type}
可收集字段: {fields}
必填字段: {required}

JSON 结构:
{{
  "draft": {{"字段": "值", "未提到的字段省略"}},
  "notes": "一句话说明你从哪里识别到的（可省略）"
}}

规则:
- 只提取用户明确说出的内容，不猜测、不编造；
- 必填字段用户没说的就省略（系统会追问补齐）；
- 日期用 YYYY-MM-DD。

<<OUTPUT_CONTRACT>>draft<<END_OUTPUT_CONTRACT>>
<<USER_TEXT>>
{user_text}
<<END_USER_TEXT>>"""

_DRAFT_INTENT = {
    "customer": "新增一个客户档案",
    "contact": "新增或补充联系人",
    "lead": "登记一条销售线索",
    "opportunity": "新增一个商机",
}

_DRAFT_FIELD_DESC = {
    "customer": "name(客户名称) customer_type(ORG/IND) industry legal_person registered_capital registered_address remark",
    "contact": "customer_name(所属客户) contact_name mobile title dept",
    "lead": "raw_content(线索内容) contact_name contact_phone biz_line region source_desc",
    "opportunity": "customer_name(所属客户) name(商机名称) biz_line expected_close_on remark",
}


def _resolve_draft_type(spec: Any) -> str:
    draft_type = getattr(spec, "draft_type", "")
    if draft_type not in DRAFT_REQUIRED_FIELDS:
        raise ValueError(f"能力 {getattr(spec, 'name', '?')} 未声明合法 draft_type: {draft_type}")
    return draft_type


async def extract_draft_node(state: ExtractGraphState, *, model, spec) -> dict[str, Any]:
    """按草稿类型调用模型整理字段；解析失败视为节点失败（不静默造假结果）。"""
    draft_type = _resolve_draft_type(spec)
    required = "、".join(label for _, label in DRAFT_REQUIRED_FIELDS[draft_type])
    prompt = DRAFT_PROMPT_TEMPLATE.format(
        intent=_DRAFT_INTENT[draft_type],
        draft_type=draft_type,
        fields=_DRAFT_FIELD_DESC[draft_type],
        required=required,
        user_text=state["user_text"],
    )
    response = await model.ainvoke(prompt)
    content = response.content if isinstance(response.content, str) else str(response.content)
    match = _JSON_RE.search(content)
    if not match:
        return {"error": "模型输出不是合法 JSON 草稿"}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"error": "模型输出 JSON 解析失败"}
    draft = payload.get("draft") if isinstance(payload, dict) else None
    if not isinstance(draft, dict):
        return {"error": "草稿结构不合法（缺少 draft 对象）"}
    draft = {k: v for k, v in draft.items() if isinstance(v, str) and v.strip()}
    return {"extraction": {"draft_type": draft_type, "draft": draft, "notes": str(payload.get("notes") or "")}}


def _missing_draft_fields(extraction: dict[str, Any]) -> list[dict[str, str]]:
    draft = extraction.get("draft") or {}
    draft_type = extraction.get("draft_type") or ""
    missing: list[dict[str, str]] = []
    for field, label in DRAFT_REQUIRED_FIELDS.get(draft_type, ()):
        if not str(draft.get(field) or "").strip():
            missing.append({"field": field, "label": label})
    return missing


async def plan_draft_node(state: ExtractGraphState) -> dict[str, Any]:
    """检查必填字段缺失：有缺失则进入等待（interrupt 由 ask_draft_missing 执行）。"""
    if state.get("error"):
        return {}
    extraction = dict(state.get("extraction") or {})
    extraction["missing"] = _missing_draft_fields(extraction)
    return {"extraction": extraction}


async def ask_draft_missing_node(state: ExtractGraphState) -> dict[str, Any]:
    """缺参追问：图原生 interrupt。恢复时重入合并应答，仍缺参则递增版本再次等待。"""
    extraction = dict(state.get("extraction") or {})
    missing = extraction.get("missing") or []
    version = int(state.get("state_version") or 0)
    if missing:
        resume_value = interrupt(
            {
                "type": "missing_input",
                "questions": [{"field": m["field"], "label": m["label"]} for m in missing],
                "state_version": version + 1,
            }
        )
        answers = strip_usage_metadata(resume_value or {})
        draft = dict(extraction.get("draft") or {})
        # 结构化应答 {"fields": {...}}；自由文本 {"text": "字段:值"} 逐行解析
        fields = answers.get("fields") if isinstance(answers.get("fields"), dict) else {}
        for key, value in fields.items():
            if isinstance(value, str) and value.strip():
                draft[key] = value.strip()
        for line in str(answers.get("text") or "").splitlines():
            if "：" in line:
                key, _, value = line.partition("：")
            elif ":" in line:
                key, _, value = line.partition(":")
            else:
                continue
            key = key.strip()
            value = value.strip()
            if key and value:
                draft[_normalize_field_key(key, extraction.get("draft_type", ""))] = value
        extraction["draft"] = draft
        extraction["missing"] = _missing_draft_fields(extraction)
        version += 1
        return {"extraction": extraction, "answers": answers, "state_version": version}
    return {}


# 追问应答的常见中文标签 → 字段名（与 DRAFT_FIELD_DESC/REQUIRED 的 label 对齐）
_DRAFT_FIELD_ALIASES: dict[str, str] = {
    "客户名称": "name",
    "名称": "name",
    "客户类型": "customer_type",
    "所属客户": "customer_name",
    "所属客户名称": "customer_name",
    "客户": "customer_name",
    "联系人": "contact_name",
    "联系人姓名": "contact_name",
    "电话": "mobile",
    "手机": "mobile",
    "职务": "title",
    "部门": "dept",
    "线索内容": "raw_content",
    "内容": "raw_content",
    "商机名称": "name",
    "行业": "industry",
    "备注": "remark",
}


def _normalize_field_key(key: str, draft_type: str) -> str:
    """应答字段键归一：中文标签/别名 → 规范字段名；未知键按原文保留（不静默丢弃）。"""
    if key in _DRAFT_FIELD_ALIASES:
        return _DRAFT_FIELD_ALIASES[key]
    return key


async def build_draft_node(state: ExtractGraphState) -> dict[str, Any]:
    """组装草稿输出（只读产物；不创建任何正式对象，确认与写入在 CRM）。"""
    extraction = dict(state.get("extraction") or {})
    result = {
        "draft": {
            "draft_type": extraction.get("draft_type"),
            "fields": extraction.get("draft", {}),
        },
        "references": {"source": "assistant.draft", "notes": extraction.get("notes", "")},
        "notes": "草稿需本人确认后由 CRM 写入正式业务；同名档案先提示已存在（先匹配再新增）。",
    }
    return {"result": strip_usage_metadata(result)}


def route_after_extract(state: ExtractGraphState) -> str:
    return END if state.get("error") else "plan_draft"


def route_after_plan(state: ExtractGraphState) -> str:
    if state.get("error"):
        return END
    extraction = state.get("extraction") or {}
    return "ask_draft_missing" if (extraction.get("missing") or []) else "build_draft"


def route_after_ask(state: ExtractGraphState) -> str:
    if state.get("error"):
        return END
    extraction = state.get("extraction") or {}
    return "ask_draft_missing" if (extraction.get("missing") or []) else "build_draft"


def build_draft_graph(spec, model: BaseChatModel) -> StateGraph:
    graph = StateGraph(ExtractGraphState)

    async def _extract(state: ExtractGraphState) -> dict:
        return await extract_draft_node(state, model=model, spec=spec)

    graph.add_node("extract_draft", _extract)
    graph.add_node("plan_draft", plan_draft_node)
    graph.add_node("ask_draft_missing", ask_draft_missing_node)
    graph.add_node("build_draft", build_draft_node)

    graph.add_edge(START, "extract_draft")
    graph.add_conditional_edges("extract_draft", route_after_extract)
    graph.add_conditional_edges("plan_draft", route_after_plan)
    graph.add_conditional_edges("ask_draft_missing", route_after_ask)
    graph.add_edge("build_draft", END)
    return graph


def compile_draft_graph(
    spec, model: BaseChatModel, checkpointer=None
) -> CompiledStateGraph:
    return build_draft_graph(spec, model).compile(checkpointer=checkpointer)
