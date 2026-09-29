"""interrupt 载荷读取：恢复前校验等待状态与 state_version（防双标签页/旧 resume）。"""
from __future__ import annotations

from typing import Any

from langgraph.graph.state import CompiledStateGraph


async def get_pending_interrupt(
    graph: CompiledStateGraph, thread_id: str
) -> dict[str, Any] | None:
    """返回线程当前挂起的 interrupt 载荷；无等待返回 None。

    使用 aget_state（AsyncPostgresSaver 禁止在事件循环线程做同步调用）；
    兼容 langgraph 各版本：任务属性为 interrupts（元组）或 interrupt（单体）。
    """
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    for task in snapshot.tasks:
        items = getattr(task, "interrupts", None)
        if items is None:
            single = getattr(task, "interrupt", None)
            items = (single,) if single is not None else ()
        for item in items:
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                return value
    return None
