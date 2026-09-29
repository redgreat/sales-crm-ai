r"""确定性 Stub 模型：仅用于契约测试与开发联调，不伪造真实模型能力。

约定：图模板用 <<USER_TEXT>>...<<END_USER_TEXT>> 包裹用户原文，
Stub 只解析其中的标记行（与真实 Provider 走同一 prompt/schema）。

解析规则（纯确定性，无智能）：
- `客户：X` / `客户: X`           -> customers
- `任务：X` / `任务: X`（每行一个）-> tasks[].title
- `负责人：X`                     -> 最近的任务/活动的 assignee_name
- ISO 日期 `\d{4}-\d{2}-\d{2}`    -> 最近的任务/活动的 due_date / occurred_at
- 文本含 `[[缺日期]]`             -> 任务不带 due_date（用于触发追问）

Prompt 含 `<<OUTPUT_CONTRACT>>analysis<<END_OUTPUT_CONTRACT>>` 时（只读分析类能力，
如 daily.draft / object.summary / today.summary / manager.focus）改为确定性归纳：
把事实行原样汇总，不做任何推断，也不生成新的业务事实；citations 只回抄事实行里的 id。
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Iterable, Optional

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_USER_TEXT_RE = re.compile(r"<<USER_TEXT>>\n?(.*?)\n?<<END_USER_TEXT>>", re.DOTALL)
_ANALYSIS_CONTRACT_RE = re.compile(r"<<OUTPUT_CONTRACT>>analysis<<END_OUTPUT_CONTRACT>>")
_FACT_LINE_RE = re.compile(r"^\[F\d+\]\s+(.*)$", re.MULTILINE)
_CUSTOMER_RE = re.compile(r"客户\s*[:：]\s*(\S+)")
_TASK_RE = re.compile(r"^任务\s*[:：]\s*(.+)$", re.MULTILINE)
_ASSIGNEE_RE = re.compile(r"负责人\s*[:：]\s*(\S+)")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_facts_text(user_text: str) -> dict[str, Any]:
    """确定性归纳：事实行原样汇总（只读，不做推断、不产生新业务事实）。

    事实行格式（由 app/graphs/analysis.py 序列化）：
        [F1] type=activity id=ACT-1 version=3 occurred_at=... text=客户拜访，讨论续约
    """
    points: list[str] = []
    citations: list[dict[str, str]] = []
    for raw_line in user_text.splitlines():
        match = _FACT_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        body = match.group(1)
        text_match = re.search(r"\btext=(.*)$", body)
        points.append(text_match.group(1).strip() if text_match else body.strip())

        def _field(name: str) -> str:
            found = re.search(rf"\b{name}=(\S+)", body)
            return found.group(1) if found else ""

        ref_id = _field("id")
        if ref_id:
            citations.append(
                {
                    "ref_id": ref_id,
                    "type": _field("type") or "other",
                    "version": _field("version"),
                }
            )
    return {
        "summary": points[0][:80] if points else "无可用事实",
        "sections": [{"title": "事实汇总", "points": points}] if points else [],
        "missing": [] if points else ["未提供任何事实"],
        "suggestions": [],
        "citations": citations,
    }


def parse_user_text(user_text: str) -> dict[str, Any]:
    customers = _CUSTOMER_RE.findall(user_text)
    dates = _DATE_RE.findall(user_text)
    task_titles = _TASK_RE.findall(user_text)
    assignees = _ASSIGNEE_RE.findall(user_text)
    missing_date = "[[缺日期]]" in user_text

    tasks: list[dict[str, Any]] = []
    for index, title in enumerate(task_titles):
        task: dict[str, Any] = {"title": title.strip()}
        if not missing_date and index < len(dates):
            task["due_date"] = dates[index]
        if index < len(assignees):
            task["assignee_name"] = assignees[index]
        if len(customers) == 1:
            task["customer_name"] = customers[0]
        tasks.append(task)

    payload: dict[str, Any] = {
        "summary": user_text.strip().replace("\n", " ")[:80],
        "customers": customers,
        "activities": (
            [{"subject": task_titles[0].strip(), "occurred_at": dates[0] if dates else None}]
            if task_titles
            else []
        ),
        "tasks": tasks,
        "missing": [],
    }
    for task in tasks:
        for field in ("customer_name", "due_date", "assignee_name"):
            if field not in task:
                payload["missing"].append({"task_title": task["title"], "field": field})
    return payload


class StubChatModel(BaseChatModel):
    """langchain BaseChatModel 适配；输出 JSON 文本，不产出 usage 元数据。"""

    model_name: str = "stub"

    @property
    def _llm_type(self) -> str:
        return "stub"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._build_result(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._build_result(messages)

    def _build_result(self, messages: list[BaseMessage]) -> ChatResult:
        prompt = messages[-1].content if messages else ""
        if not isinstance(prompt, str):
            prompt = str(prompt)
        match = _USER_TEXT_RE.search(prompt)
        user_text = match.group(1) if match else prompt
        payload = (
            parse_facts_text(user_text)
            if _ANALYSIS_CONTRACT_RE.search(prompt)
            else parse_user_text(user_text)
        )
        message = AIMessage(content=json.dumps(payload, ensure_ascii=False))
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def astream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        yield self._build_result(messages).generations[0].message

    def bind_tools(self, tools: Iterable[Any], **kwargs: Any):  # pragma: no cover
        raise NotImplementedError("Stub 模型不支持工具调用")


# 供应商响应中携带的 usage/token 计费字段（一律剥离，不落日志/消息/checkpoint）
USAGE_METADATA_KEYS = ("usage_metadata", "response_metadata", "token_usage", "usage")


def strip_usage_metadata(payload: Any) -> Any:
    """递归剥离 usage/token/cost 相关键。用于任何进入消息、结果或 checkpoint 的数据。"""
    if isinstance(payload, dict):
        return {
            key: strip_usage_metadata(value)
            for key, value in payload.items()
            if key.lower() not in USAGE_METADATA_KEYS
            and not any(token in key.lower() for token in ("token", "cost"))
        }
    if isinstance(payload, list):
        return [strip_usage_metadata(item) for item in payload]
    return payload
