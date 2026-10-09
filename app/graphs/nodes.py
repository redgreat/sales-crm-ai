"""communication.extract 图节点：抽取 → 缺参检查 → interrupt 追问 → 组装候选。

候选只是建议（零到多活动/任务建议），不写任何正式业务；
本人确认与正式写入由 CRM 完成（需求 4.1、信任边界）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from langgraph.types import interrupt

from app.graphs.state import ExtractGraphState
from app.providers.stub import strip_usage_metadata

from langgraph.graph import END

PROMPT_TEMPLATE = """你是 CRM 销售助理。从下面的沟通记录中抽取客户/活动/任务建议，只输出 JSON，不要输出其他文字。

JSON 结构:
{{
  "summary": "一句话概述",
  "customers": ["客户名"],
  "activities": [{{"subject": "...", "occurred_at": "YYYY-MM-DD 或 null"}}],
  "tasks": [{{"title": "...", "due_date": "YYYY-MM-DD 或省略", "assignee_name": "省略则未知", "customer_name": "省略则未知"}}],
  "missing": [{{"task_title": "...", "field": "customer_name|due_date|assignee_name"}}]
}}

<<USER_TEXT>>
{user_text}
<<END_USER_TEXT>>"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# 任务候选必填字段；缺失则触发 interrupt 追问
REQUIRED_TASK_FIELDS = ("customer_name", "due_date", "assignee_name")

_ANSWER_FIELD_ALIASES = {
    "客户": "customer_name",
    "客户名称": "customer_name",
    "customer": "customer_name",
    "日期": "due_date",
    "截止": "due_date",
    "截止日期": "due_date",
    "due": "due_date",
    "负责人": "assignee_name",
    "assignee": "assignee_name",
}


async def extract_node(state: ExtractGraphState, *, model) -> dict[str, Any]:
    """调用模型抽取；解析失败视为节点失败（不静默造假结果）。"""
    prompt = PROMPT_TEMPLATE.format(user_text=state["user_text"])
    response = await model.ainvoke(prompt)
    content = response.content if isinstance(response.content, str) else str(response.content)
    match = _JSON_RE.search(content)
    if not match:
        return {"error": "模型输出不是合法 JSON 抽取结果"}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"error": "模型输出 JSON 解析失败"}
    return {"extraction": strip_usage_metadata(payload)}


def _missing_fields(extraction: dict[str, Any]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for task in extraction.get("tasks", []):
        for field in REQUIRED_TASK_FIELDS:
            if not task.get(field):
                missing.append({"task_title": task.get("title", ""), "field": field})
    return missing


async def plan_candidates_node(state: ExtractGraphState) -> dict[str, Any]:
    """检查缺参：有缺失则进入等待（interrupt 由 ask_missing 节点执行）。"""
    if state.get("error"):
        return {}
    extraction = dict(state.get("extraction") or {})
    extraction["missing"] = _missing_fields(extraction)
    return {"extraction": extraction}


async def ask_missing_node(state: ExtractGraphState) -> dict[str, Any]:
    """缺参追问：图原生 interrupt，等待不占 worker。

    恢复时节点重入，interrupt() 返回应答；合并后重新校验，
    仍缺参则用递增后的 state_version 再次 interrupt（防旧 resume 重放）。
    """
    extraction = dict(state.get("extraction") or {})
    missing = extraction.get("missing") or []
    version = int(state.get("state_version") or 0)
    if missing:
        resume_value = interrupt(
            {
                "type": "missing_input",
                "questions": [
                    {"task_title": m["task_title"], "field": m["field"]} for m in missing
                ],
                "state_version": version + 1,
            }
        )
        answers = strip_usage_metadata(_normalize_answers(resume_value or {}))
        extraction = _merge_answers(extraction, answers)
        extraction["missing"] = _missing_fields(extraction)
        version += 1
        if extraction["missing"]:
            # 应答仍不完整：下一轮继续问（回到本节点前 plan_candidates 会再进入 ask）
            return {"extraction": extraction, "answers": answers, "state_version": version}
        return {"extraction": extraction, "answers": answers, "state_version": version}
    return {}


async def build_candidates_node(state: ExtractGraphState) -> dict[str, Any]:
    """组装最终候选建议（只读输出；不创建任何正式对象）。"""
    extraction = dict(state.get("extraction") or {})
    customers = extraction.get("customers", [])
    summary = str(extraction.get("summary") or "").strip() or str(state.get("user_text") or "").strip()
    result = {
        "summary": summary,
        "candidates": {
            "customers": customers,
            "activities": extraction.get("activities", []),
            "tasks": extraction.get("tasks", []),
        },
        "references": {
            "customers": customers,
            "source": "communication.extract",
        },
        "notes": "候选建议需 CRM 用户本人确认后才会写入正式业务。",
    }
    return {"result": strip_usage_metadata(result)}


def route_after_extract(state: ExtractGraphState) -> str:
    return END if state.get("error") else "plan_candidates"


def route_after_plan(state: ExtractGraphState) -> str:
    if state.get("error"):
        return END
    extraction = state.get("extraction") or {}
    return "ask_missing" if (extraction.get("missing") or []) else "build_candidates"


def route_after_ask(state: ExtractGraphState) -> str:
    if state.get("error"):
        return END
    extraction = state.get("extraction") or {}
    return "ask_missing" if (extraction.get("missing") or []) else "build_candidates"


def _normalize_answers(value: Any) -> dict[str, Any]:
    """把 resume 值归一成 {task_title: {field: value}} 或全局 {field: value}。"""
    if not isinstance(value, dict):
        return {"text": str(value)}
    return value


def _merge_answers(
    extraction: dict[str, Any], answers: dict[str, Any]
) -> dict[str, Any]:
    """把用户应答合并进抽取结果。

    支持两种形态：
    - 结构化：{"tasks": [{"title": ..., "due_date": ...}]} / {"due_date": "..."}（全局补齐）
    - 自由文本：{"text": "日期：2026-10-01\n负责人：张三"}（逐行 `字段:值` 解析，裸 ISO 日期视为 due_date）
    """
    merged = dict(extraction)
    tasks = [dict(t) for t in merged.get("tasks", [])]

    structured_fields: dict[str, str] = {}
    per_task: dict[str, dict[str, str]] = {}
    free_text = str(answers.get("text") or "").strip()

    for key, value in answers.items():
        if key == "text":
            continue
        if key == "tasks" and isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("title"):
                    per_task[item["title"]] = {
                        k: str(v) for k, v in item.items() if k != "title" and v
                    }
        elif isinstance(value, str) and value.strip():
            field = _ANSWER_FIELD_ALIASES.get(key.lower(), key)
            structured_fields[field] = value.strip()

    if free_text:
        for line in free_text.splitlines():
            line = line.strip()
            if not line:
                continue
            alias_match = re.match(r"^(\S+?)\s*[:：]\s*(.+)$", line)
            if alias_match:
                field = _ANSWER_FIELD_ALIASES.get(
                    alias_match.group(1).lower(), alias_match.group(1).lower()
                )
                structured_fields[field] = alias_match.group(2).strip()
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", line):
                structured_fields["due_date"] = line

    for task in tasks:
        title = task.get("title", "")
        overrides = dict(per_task.get(title, {}))
        for field, value in structured_fields.items():
            if not task.get(field):
                overrides.setdefault(field, value)
        task.update(overrides)

    merged["tasks"] = tasks
    return merged
