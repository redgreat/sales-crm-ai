"""图逻辑单元测试（MemorySaver 仅用于逻辑验证；持久化/恢复行为由真实 PG 测试覆盖）。"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graphs.builder import compile_graph
from app.providers.stub import StubChatModel


async def _invoke(graph, text: str, thread_id: str = "t1"):
    config = {"configurable": {"thread_id": thread_id}}
    return await graph.ainvoke({"user_text": text}, config=config), config


async def test_complete_text_builds_candidates():
    graph = compile_graph(StubChatModel(), checkpointer=MemorySaver())
    final, _ = await _invoke(graph, "客户：ACME公司\n任务：电话回访\n负责人：张三\n2026-10-01")
    result = final["result"]
    assert result["candidates"]["tasks"][0]["title"] == "电话回访"
    assert result["candidates"]["tasks"][0]["due_date"] == "2026-10-01"
    assert "本人确认" in result["notes"]


async def test_missing_fields_interrupts():
    graph = compile_graph(StubChatModel(), checkpointer=MemorySaver())
    final, config = await _invoke(graph, "[[缺日期]]\n任务：电话跟进")
    assert "__interrupt__" in final
    payload = final["__interrupt__"][0].value
    assert payload["type"] == "missing_input"
    assert payload["state_version"] == 1

    # 恢复：补齐日期（客户/负责人用全局应答）
    resumed = await graph.ainvoke(
        Command(resume={"text": "日期：2026-10-09\n负责人：李四\n客户：ACME"}), config=config
    )
    assert resumed["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-09"
    assert resumed["result"]["candidates"]["tasks"][0]["assignee_name"] == "李四"


async def test_resume_state_version_increments_for_partial_answers():
    graph = compile_graph(StubChatModel(), checkpointer=MemorySaver())
    final, config = await _invoke(graph, "[[缺日期]]\n任务：拜访")
    assert "__interrupt__" in final
    # 部分应答：只给日期，仍缺客户/负责人 → 再次 interrupt，版本递增
    second = await graph.ainvoke(Command(resume={"text": "2026-10-02"}), config=config)
    payload = second["__interrupt__"][0].value
    assert payload["state_version"] == 2
    fields = {q["field"] for q in payload["questions"]}
    assert fields == {"customer_name", "assignee_name"}


async def test_model_bad_output_marks_error():
    class BadModel(StubChatModel):
        def _build_result(self, messages):
            from langchain_core.messages import AIMessage
            from langchain_core.outputs import ChatGeneration, ChatResult

            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="not json"))])

    graph = compile_graph(BadModel(), checkpointer=MemorySaver())
    final, _ = await _invoke(graph, "随便说点什么，没有标记行")
    assert final.get("error")
