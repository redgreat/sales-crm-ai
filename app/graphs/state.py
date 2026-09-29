"""图状态定义（形态参考 AgentZR problem_agent graph/state.py，提交 b14adec）。

约束：
- 模型原始返回不进状态，只有结构化解析结果（供应商 usage 因此不进 checkpoint）；
- state_version 每次进入等待/补参时递增，恢复请求必须回带同版本（防双标签页/旧 resume）。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class ExtractGraphState(TypedDict, total=False):
    # Run 级上下文（executor 注入）：能力名与 CRM 入参（P5 起分析图/问答图共用）
    capability: str
    input_payload: dict[str, Any] | None
    # 会话消息（有限窗口由调用方裁剪；本图最小实现只带当轮）
    messages: Annotated[list[Any], add_messages]
    # 当轮用户输入（CRM 装配好的事实文本）
    user_text: str
    # 模型结构化抽取结果（dict，符合 ExtractionPayload schema）
    extraction: dict[str, Any] | None
    # 缺参追问应答（resume 值合并后的抽取结果）
    answers: dict[str, Any] | None
    # 补参状态版本：每次 interrupt 递增，resume 必须携带匹配版本
    state_version: int
    # 最终输出（候选建议，不写任何正式业务）
    result: dict[str, Any] | None
    # 错误信息（节点失败时填充，executor 转为 run 失败）
    error: str | None


GRAPH_VERSION = "extract@1"
PROMPT_VERSION = "extract-prompt@1"
