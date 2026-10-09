"""Run 入参上限：在调用模型/识别供应商之前拒绝超大、超量、过深嵌套的输入。

为什么放在路由层而不是等模型报错：
- 超限输入若先入队，会占用一次 attempt、把超长原文写进 `ai_runs.input` 与 checkpoint；
- 供应商返回的错误报文常包含完整请求体，回显会把用户原文带进日志与错误信息；
- 上限是"能力契约之外"的统一底线，各能力更严的上限仍由注册表 `max_facts` 声明。
"""
from __future__ import annotations

import json
from typing import Any

from app.errors import PayloadTooLarge

# 单段文本：与会话消息上限（20000 字符）保持一致，避免两个入口口径不一
MAX_TEXT_CHARS = 20_000
MAX_TEXT_BYTES = 64 * 1024
# 事实条数硬上限（注册表 max_facts 更严时以注册表为准）
MAX_FACTS_ITEMS = 200
# 单条事实序列化上限，防止一条巨型 fact 撑爆上下文
MAX_FACT_BYTES = 8 * 1024
# 任意数组条数上限（facts 另有更严上限）
MAX_LIST_ITEMS = 500
# 整个 input 序列化上限
MAX_INPUT_BYTES = 512 * 1024
# 补参（resume）内容上限
MAX_RESUME_VALUES_BYTES = 64 * 1024
# 嵌套深度上限：防御递归结构与序列化爆炸
MAX_DEPTH = 8


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _check_text(value: str, path: str) -> None:
    if len(value) > MAX_TEXT_CHARS:
        raise PayloadTooLarge(
            f"{path} 超过 {MAX_TEXT_CHARS} 字符上限",
            details={"path": path, "limit": MAX_TEXT_CHARS, "length": len(value)},
        )
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise PayloadTooLarge(
            f"{path} 超过 {MAX_TEXT_BYTES} 字节上限",
            details={"path": path, "limit": MAX_TEXT_BYTES, "bytes": len(value.encode('utf-8'))},
        )


def _check_node(node: Any, path: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise PayloadTooLarge(f"{path} 嵌套层级超过 {MAX_DEPTH} 层", details={"path": path})
    if isinstance(node, str):
        _check_text(node, path)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            _check_node(value, f"{path}.{key}", depth + 1)
        return
    if isinstance(node, (list, tuple)):
        if len(node) > MAX_LIST_ITEMS:
            raise PayloadTooLarge(
                f"{path} 超过 {MAX_LIST_ITEMS} 条上限",
                details={"path": path, "limit": MAX_LIST_ITEMS, "count": len(node)},
            )
        for index, value in enumerate(node):
            _check_node(value, f"{path}[{index}]", depth + 1)


def validate_run_input(payload: dict[str, Any]) -> None:
    """校验 `CreateRunBody.input`：总量、文本、条数与结构深度。"""
    if not isinstance(payload, dict):
        raise PayloadTooLarge("input 必须是对象")
    total = _json_bytes(payload)
    if total > MAX_INPUT_BYTES:
        raise PayloadTooLarge(
            f"input 序列化后 {total} 字节，超过 {MAX_INPUT_BYTES} 字节上限",
            details={"limit": MAX_INPUT_BYTES, "bytes": total},
        )
    facts = payload.get("facts")
    if isinstance(facts, list) and len(facts) > MAX_FACTS_ITEMS:
        raise PayloadTooLarge(
            f"input.facts 超过 {MAX_FACTS_ITEMS} 条硬上限",
            details={"limit": MAX_FACTS_ITEMS, "count": len(facts)},
        )
    for key, value in payload.items():
        _check_node(value, f"input.{key}", 1)
    if isinstance(facts, list):
        for index, fact in enumerate(facts):
            size = _json_bytes(fact)
            if size > MAX_FACT_BYTES:
                raise PayloadTooLarge(
                    f"input.facts[{index}] 超过单条 {MAX_FACT_BYTES} 字节上限",
                    details={"index": index, "limit": MAX_FACT_BYTES, "bytes": size},
                )


def validate_resume_values(values: dict[str, Any]) -> None:
    """校验补参内容（resume.values）：补参同样会进入模型上下文与 checkpoint。"""
    if not isinstance(values, dict):
        raise PayloadTooLarge("values 必须是对象")
    total = _json_bytes(values)
    if total > MAX_RESUME_VALUES_BYTES:
        raise PayloadTooLarge(
            f"补参内容超过 {MAX_RESUME_VALUES_BYTES} 字节上限",
            details={"limit": MAX_RESUME_VALUES_BYTES, "bytes": total},
        )
    for key, value in values.items():
        _check_node(value, f"values.{key}", 1)
