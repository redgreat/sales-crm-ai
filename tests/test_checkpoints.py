"""checkpoint 真实 PostgreSQL 重启恢复测试（需求 5.2 / 9）。

流程：图跑进 interrupt → 停库（fast）→ 起库 → 重新打开 saver →
等待状态仍在 → 恢复 → 结果完整。checkpoint 不丢、不降级。
"""
from __future__ import annotations

import pytest
from langgraph.types import Command

from app.graphs.builder import compile_graph
from app.persistence.checkpoints import open_postgres_saver
from app.providers.stub import StubChatModel

pytestmark = pytest.mark.realpg

TEXT_MISSING_DATE = "[[缺日期]]\n任务：电话跟进\n客户：ACME\n负责人：张三"
COMPLETE_ANSWER = {"text": "日期：2026-10-05"}


async def test_checkpoint_survives_postgres_restart(db_url: str, pg_cluster):
    thread_id = "conv-restart-1"
    config = {"configurable": {"thread_id": thread_id}}

    async with open_postgres_saver(db_url) as saver:
        graph = compile_graph(StubChatModel(), checkpointer=saver)
        final = await graph.ainvoke({"user_text": TEXT_MISSING_DATE}, config=config)
        assert "__interrupt__" in final
        payload = final["__interrupt__"][0].value
        assert payload["state_version"] == 1

    # 真实重启 PostgreSQL
    pg_cluster.restart(mode="fast")

    async with open_postgres_saver(db_url) as saver:
        graph = compile_graph(StubChatModel(), checkpointer=saver)
        state = await graph.aget_state(config)
        assert state.next, "重启后 checkpoint 仍应处于等待状态"
        resumed = await graph.ainvoke(Command(resume=COMPLETE_ANSWER), config=config)
        assert resumed["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-05"
        assert resumed["result"]["candidates"]["tasks"][0]["assignee_name"] == "张三"


async def test_duplicate_resume_rejected_after_consumed(db_url: str):
    """重复 resume：等待状态被消费后再 resume，无挂起 interrupt 可供恢复。"""
    from app.graphs.interrupts import get_pending_interrupt

    thread_id = "conv-dup-resume"
    config = {"configurable": {"thread_id": thread_id}}
    async with open_postgres_saver(db_url) as saver:
        graph = compile_graph(StubChatModel(), checkpointer=saver)
        final = await graph.ainvoke({"user_text": TEXT_MISSING_DATE}, config=config)
        assert "__interrupt__" in final
        assert await get_pending_interrupt(graph, thread_id) is not None

        await graph.ainvoke(Command(resume=COMPLETE_ANSWER), config=config)
        # 已完成：无挂起 interrupt → get_pending_interrupt 返回 None（上层据此拒绝重复 resume）
        assert await get_pending_interrupt(graph, thread_id) is None
