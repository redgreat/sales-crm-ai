"""图构建与入口工厂（模式参考 AgentZR problem_agent graph/builder.py，提交 b14adec）。

open_compiled_graph 让持久化图与数据库连接共享生命周期；
checkpoint 结构不满足时抛错，不降级为无状态图。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from functools import lru_cache
from typing import AsyncIterator

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.capabilities import DRAFT_GRAPH_VERSION, QA_GRAPH_VERSION, capability_versions, get_capability
from app.graphs.analysis import build_analysis_graph, compile_analysis_graph
from app.graphs.draft import build_draft_graph, compile_draft_graph
from app.graphs.qa import build_qa_graph, compile_qa_graph
from app.graphs.nodes import (
    ask_missing_node,
    build_candidates_node,
    extract_node,
    plan_candidates_node,
    route_after_ask,
    route_after_extract,
    route_after_plan,
)
from app.graphs.state import ExtractGraphState
from app.persistence.checkpoints import open_postgres_saver


def build_graph(model: BaseChatModel) -> StateGraph:
    graph = StateGraph(ExtractGraphState)

    async def _extract(state: ExtractGraphState) -> dict:
        return await extract_node(state, model=model)

    graph.add_node("extract", _extract)
    graph.add_node("plan_candidates", plan_candidates_node)
    graph.add_node("ask_missing", ask_missing_node)
    graph.add_node("build_candidates", build_candidates_node)

    graph.add_edge(START, "extract")
    graph.add_conditional_edges("extract", route_after_extract)
    graph.add_conditional_edges("plan_candidates", route_after_plan)
    graph.add_conditional_edges("ask_missing", route_after_ask)
    graph.add_edge("build_candidates", END)
    return graph


def compile_graph(model: BaseChatModel, checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    return build_graph(model).compile(checkpointer=checkpointer)


@lru_cache(maxsize=1)
def graph_registry_version() -> dict[str, str]:
    """能力 → 图版本登记（需求 5.1：记录图版本）。来源：app/capabilities.py 注册表。"""
    return capability_versions()


def build_graph_for(capability: str, model: BaseChatModel) -> StateGraph:
    """按能力注册表建图：抽取型走 extract 图，问答族走 QA 图，其余走只读分析图。"""
    spec = get_capability(capability)
    if spec is None:
        raise ValueError(f"未知能力: {capability}")
    if spec.kind == "extract" and spec.graph_version == DRAFT_GRAPH_VERSION:
        return build_draft_graph(spec, model)
    if spec.kind == "extract":
        return build_graph(model)
    if not spec.prompt_template:
        raise ValueError(f"能力 {capability} 未登记 Prompt 模板")
    if spec.graph_version == QA_GRAPH_VERSION:
        return build_qa_graph(spec, model)
    return build_analysis_graph(spec, model)


def compile_graph_for(
    capability: str, model: BaseChatModel, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    spec = get_capability(capability)
    if spec is None:
        raise ValueError(f"未知能力: {capability}")
    if spec.kind == "extract" and spec.graph_version == DRAFT_GRAPH_VERSION:
        return compile_draft_graph(spec, model, checkpointer=checkpointer)
    if spec.kind == "extract":
        return compile_graph(model, checkpointer=checkpointer)
    if spec.graph_version == QA_GRAPH_VERSION:
        return compile_qa_graph(spec, model, checkpointer=checkpointer)
    return compile_analysis_graph(spec, model, checkpointer=checkpointer)


@asynccontextmanager
async def open_compiled_graph(
    checkpoint_conn_string: str, model: BaseChatModel
) -> AsyncIterator[CompiledStateGraph]:
    """持久化编译图；调用方持有连接生命周期。"""
    async with open_postgres_saver(checkpoint_conn_string) as saver:
        yield compile_graph(model, checkpointer=saver)
