"""移动端 PRD 对齐（M-01 / M-10 / M-12）在 AI 项目的落实测试。

来源：docs/AI接入需求文档.md 第 11.5 节。本文件只验证 AI 侧可验证的部分：
- M-01：未确认候选不得作为事实入模；
- M-10：只检索已发布当前版本（不靠 TEXT 版本号排序）、授权范围失败即关闭、
        撤权后旧回答不得借会话历史再次进入模型；
- M-12：能力目录给出能力码/只读标识/可见条件/返回路径，且由注册表渲染。

不联网、不依赖真实模型；持久化用例用真实 PG（conftest 提供的单实例）。
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.api.main import create_app
from app.capabilities import CAPABILITIES
from app.config import Settings
from app.graphs.builder import compile_graph_for
from app.graphs.fact_guard import find_unconfirmed_fact, unconfirmed_reason
from app.knowledge import (
    authorized_knowledge_ids,
    cited_knowledge_ids,
    filter_unauthorized_history,
    validate_scope,
)
from app.providers.stub import StubChatModel

OK_FACT = {"type": "activity", "id": "ACT-1", "version": "1", "text": "拜访 ACME"}


def _graph(capability: str):
    return compile_graph_for(capability, StubChatModel(), checkpointer=MemorySaver())


# ------------------------------------------------------------------ M-01


@pytest.mark.parametrize(
    "fact",
    [
        {"type": "candidate", "id": "C-1", "text": "建议周三拜访"},
        {"type": "ai_candidate", "id": "C-2", "text": "疑似商机"},
        {"type": "activity", "id": "C-3", "text": "已确认的活动", "confirmed": False},
        {"type": "task", "id": "C-4", "text": "待确认任务", "status": "待确认"},
        {"type": "task", "id": "C-5", "text": "未确认", "status": "pending"},
    ],
)
def test_unconfirmed_candidates_rejected_as_facts(fact):
    """M-01：候选不计入正式事实——任何候选标记都必须被识别。"""
    assert unconfirmed_reason(fact) is not None
    assert find_unconfirmed_fact([OK_FACT, fact]) is not None


def test_confirmed_facts_pass_guard():
    """正式事实（含显式 confirmed=true）不受影响，避免误伤。"""
    assert unconfirmed_reason(OK_FACT) is None
    assert unconfirmed_reason({**OK_FACT, "confirmed": True}) is None
    assert find_unconfirmed_fact([OK_FACT, {"type": "task", "id": "T-1", "text": "报价"}]) is None


async def test_daily_draft_refuses_candidate_facts():
    """日报底稿混入候选 → 直接失败，不送模型（M-01/M-09）。"""
    payload = {
        "facts": [OK_FACT, {"type": "candidate", "id": "C-1", "text": "建议周三拜访"}],
        "window": {"from": "2026-09-29", "to": "2026-09-29"},
        # 日报是预生成能力，先满足受益人/调度键校验，才能触达事实守卫
        "beneficiary": {"user_id": "u1"},
        "schedule_key": "2026-09-29-u1-daily",
    }
    state = await _graph("daily.draft").ainvoke(
        {"capability": "daily.draft", "input_payload": payload},
        config={"configurable": {"thread_id": "m01-draft"}},
    )
    assert state["error"]
    assert "候选" in state["error"]


async def test_qa_refuses_candidate_facts():
    """问答族同样拒绝候选作为事实（M-01）。"""
    payload = {
        "question": "续约谈到哪一步了？",
        "facts": [{"type": "candidate", "id": "C-1", "text": "疑似商机"}],
        "scope": {"subject_type": "customer", "subject_id": "C-9"},
    }
    state = await _graph("object.qa").ainvoke(
        {"capability": "object.qa", "input_payload": payload},
        config={"configurable": {"thread_id": "m01-qa"}},
    )
    assert state["error"]
    assert "候选" in state["error"]


# ------------------------------------------------------------------ M-10


def test_scope_rejects_org_dimensions():
    """M-10：角色/业务线组合不在 AI 侧计算——出现即拒绝，不做并集放大。"""
    validate_scope({"public": True})
    validate_scope({"users": ["u1"]})
    for bad in ({"roles": ["sales"]}, {"biz_lines": ["A"], "users": ["u1"]}, {"depts": ["D1"]}):
        with pytest.raises(ValueError, match="public / users"):
            validate_scope(bad)


def test_scope_rejects_empty():
    with pytest.raises(ValueError):
        validate_scope({})


async def test_history_drops_revoked_knowledge(monkeypatch):
    """M-10：引用已撤权知识的旧回答必须从历史剔除（不能借历史绕回模型）。"""

    class _FakeConn:  # 只需满足函数签名；授权判定用 monkeypatch 注入
        pass

    async def _authorized(conn, *, user_id, knowledge_ids):
        return {"kb-ok"}  # kb-revoked 已被撤权

    monkeypatch.setattr(
        "app.knowledge.store.authorized_knowledge_ids", _authorized
    )
    history = [
        {"role": "user", "content": "续约政策是什么？"},
        {"role": "assistant", "content": "政策是 9 折。", "meta": {"knowledge_refs": ["kb-ok"]}},
        {"role": "assistant", "content": "旧政策是 8 折。", "meta": {"knowledge_refs": ["kb-revoked"]}},
    ]
    import app.knowledge.store as store

    kept, dropped = await store.filter_unauthorized_history(
        _FakeConn(), user_id="u1", history=history
    )
    assert [m["content"] for m in kept] == ["续约政策是什么？", "政策是 9 折。"]
    assert dropped == ["kb-revoked"]


def test_cited_knowledge_ids_parses_forms():
    assert cited_knowledge_ids({"meta": {"knowledge_refs": ["kb-1"]}}) == ["kb-1"]
    assert cited_knowledge_ids({"meta": {"knowledge_refs": [{"ref_id": "kb-2"}]}}) == ["kb-2"]
    assert cited_knowledge_ids({"meta": '"not-dict"'}) == []
    assert cited_knowledge_ids({}) == []


@pytest.mark.realpg
async def test_search_prefers_current_version_over_text_order(db_pool):
    """M-10：v10 与 v9 的文本序陷阱——当前版本由 is_current 决定，不靠版本号排序。

    若仍按 version DESC 取，'v9' > 'v10' 会选到旧版本，这是必须防住的 bug。
    """
    from app.knowledge import upsert_document

    async with db_pool.connection() as conn:
        await upsert_document(
            conn, knowledge_id="kb-order", version="v10", title="新政策",
            content="当前：8.5 折。", scope={"users": ["u-order"]}, is_current=True,
        )
        await upsert_document(
            conn, knowledge_id="kb-order", version="v9", title="旧政策",
            content="旧：9 折。", scope={"users": ["u-order"]},
        )
        from app.knowledge import search_authorized

        docs = await search_authorized(conn, user_id="u-order", limit=5)
    assert [d["version"] for d in docs] == ["v10"]
    assert "8.5 折" in docs[0]["content"]


@pytest.mark.realpg
async def test_disable_clears_current_and_blocks_retry(db_pool):
    """M-10：撤权立即生效，且不再被判为有权/当前。"""
    from app.knowledge import disable_knowledge, search_authorized, upsert_document

    async with db_pool.connection() as conn:
        await upsert_document(
            conn, knowledge_id="kb-revoke", version="v1", content="机密政策。",
            scope={"users": ["u-revoke"]}, is_current=True,
        )
        assert await authorized_knowledge_ids(
            conn, user_id="u-revoke", knowledge_ids=["kb-revoke"]
        ) == {"kb-revoke"}
        await disable_knowledge(conn, "kb-revoke")
        assert await authorized_knowledge_ids(
            conn, user_id="u-revoke", knowledge_ids=["kb-revoke"]
        ) == set()
        assert await search_authorized(conn, user_id="u-revoke", limit=5) == []


# ------------------------------------------------------------------ M-12


def _catalog_settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql://postgres@127.0.0.1:1/x",
        auth={"service_key_id": "crm-ai", "service_secret": "test-secret"},
    )


def _dev_headers() -> dict[str, str]:
    """test 环境允许无签名调用（见 deps.require_operator），避开数据库连接。"""
    return {"x-sai-user-id": "u1", "x-sai-user-name": "tester"}


async def test_capability_catalog_exposes_entry_contract():
    """M-12：能力目录返回能力码、只读标识、可见条件与返回路径。"""
    settings = _catalog_settings()
    app = create_app(settings)
    # 能力目录不依赖数据库；这里不走 lifespan，手动挂上认证所需的 settings
    app.state.settings = settings
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/capabilities", headers=_dev_headers())
    assert response.status_code == 200
    body = response.json()
    names = {item["capability"] for item in body["capabilities"]}
    assert names == set(CAPABILITIES)
    for item in body["capabilities"]:
        assert item["read_only"] is True  # 一期全部只读，前端不得展示确认写入
        assert item["entry_conditions"], f"{item['capability']} 缺少可见条件"
        assert item["return_path"], f"{item['capability']} 缺少返回路径"
        assert item["graph_version"] and item["prompt_version"]


async def test_capability_catalog_unknown_returns_error():
    settings = _catalog_settings()
    app = create_app(settings)
    # 能力目录不依赖数据库；这里不走 lifespan，手动挂上认证所需的 settings
    app.state.settings = settings
    import httpx

    path = "/api/v1/capabilities/not.exist"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=_dev_headers())
    # 与创建 Run 保持一致：未登记能力一律 CAPABILITY_UNKNOWN（400），不做隐式兜底
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CAPABILITY_UNKNOWN"


# ------------------------------------------------------------------ M-09


async def test_daily_draft_result_carries_as_of_and_facts_count():
    """M-09：底稿必须带"截至时间"与事实条数，界面才能提示刷新后新增了多少事实。"""
    payload = {
        "facts": [OK_FACT, {"type": "task", "id": "T-1", "version": "1", "text": "提交报价"}],
        "window": {"from": "2026-09-30", "to": "2026-09-30"},
        "beneficiary": {"user_id": "u1"},
        "schedule_key": "2026-09-30-u1-daily",
    }
    state = await _graph("daily.draft").ainvoke(
        {"capability": "daily.draft", "input_payload": payload},
        config={"configurable": {"thread_id": "m09-draft"}},
    )
    result = state["result"]
    assert result["as_of"] == {"from": "2026-09-30", "to": "2026-09-30"}
    assert result["facts_count"] == 2
    assert result["read_only"] is True
    # 受益人回显：预生成结果不能被张冠李戴
    assert result["beneficiary"] == {"user_id": "u1"}


# ------------------------------------------------- 上下文预算（长会话保护）


def test_history_budget_keeps_most_recent_only():
    """长会话只保留最近若干条：历史是"最近语境"，不能挤掉事实与知识。"""
    from app.graphs.qa import _HISTORY_MAX_ITEMS, serialize_history

    history = []
    for i in range(1, 61):
        history.append({"role": "user", "content": f"第{i}轮提问"})
        history.append({"role": "assistant", "content": f"第{i}轮回答"})

    rendered = serialize_history(history)
    assert "第1轮提问" not in rendered
    assert "第60轮回答" in rendered
    assert rendered.count("[H") <= _HISTORY_MAX_ITEMS


def test_history_budget_caps_total_chars():
    """总字符数封顶；最近的一条必须保留。"""
    from app.graphs.qa import _HISTORY_MAX_CHARS, serialize_history

    history = [{"role": "user", "content": "字" * 900} for _ in range(40)]
    rendered = serialize_history(history)
    # 单条先截断到 500，预算 6000 → 约 12 条
    bodies = [ln.split("text=", 1)[1] for ln in rendered.splitlines()]
    assert sum(len(b) for b in bodies) <= _HISTORY_MAX_CHARS
    assert rendered.startswith("[H1]")
    assert rendered.count("[H") < 40
