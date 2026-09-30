"""P5 问答族测试：对象内追问 / 跨对象问答 / 知识问答 / 会前准备（需求 4.6～4.9）。

覆盖：知识发布/停用的授权同步、检索授权过滤（内容进模型前）、版本引用、
撤权立即生效、跨用户隔离、会话绑定、条数/时间窗约束、输入校验。
图逻辑用 MemorySaver 直接跑（检索注入内存桩）；持久化/多轮用真实 PG。
"""
from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from langgraph.checkpoint.memory import MemorySaver

from app.api.main import create_app
from app.auth import OperatorContext, sign_request
from app.capabilities import CAPABILITIES, get_capability, pregen_capabilities
from app.config import Settings
from app.graphs.builder import compile_graph_for
from app.providers.stub import StubChatModel

FACTS = [
    {"type": "activity", "id": "ACT-9", "version": "1",
     "occurred_at": "2026-09-29", "text": "拜访 ACME，讨论续约价格"},
    {"type": "task", "id": "TASK-9", "version": "2", "text": "下周三前提交续约报价单"},
]

QUESTION_INPUT = {
    "question": "续约谈到哪一步了？",
    "facts": FACTS,
    "scope": {"subject_type": "customer", "subject_id": "C-9"},
}

KNOW_DOC = {
    "knowledge_id": "kb-pricing",
    "version": "v2",
    "title": "续约价格政策",
    "content": "续约客户享受 9 折，满 50 万再减 2 万。",
    "status": "published",
    "scope": {"users": ["u1"]},
}


async def _run_graph(
    capability: str, payload: dict, model=None, *,
    knowledge_facts: list | None = None, history: list | None = None,
    thread_id: str = "t1",
):
    graph = compile_graph_for(capability, model or StubChatModel(), checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": thread_id}}
    initial: dict = {"capability": capability, "input_payload": payload}
    if knowledge_facts is not None:
        initial["knowledge_facts"] = knowledge_facts
    if history is not None:
        initial["history"] = history
    return await graph.ainvoke(initial, config=config)


def test_qa_registry_contract():
    assert set(CAPABILITIES) >= {"object.qa", "business.qa", "knowledge.qa", "meeting.prepare"}
    # 全部只读、非预生成；预生成集合不变（仅日报/今日任务/主管关注）
    for name in ("object.qa", "business.qa", "knowledge.qa", "meeting.prepare"):
        spec = get_capability(name)
        assert spec is not None
        assert spec.kind == "analysis"
        assert spec.read_only is True
        assert spec.pregen is False
        assert spec.graph_version == "analysis-qa@1"
        assert spec.prompt_template
    assert set(pregen_capabilities()) == {"daily.draft", "today.summary", "manager.focus"}
    assert get_capability("object.qa").requires_conversation is True
    assert get_capability("object.qa").requires_subject_binding is True
    assert get_capability("object.qa").requires_history is True
    assert get_capability("knowledge.qa").requires_facts is False
    assert get_capability("knowledge.qa").retrieves_knowledge is True
    assert get_capability("meeting.prepare").requires_facts is True
    assert get_capability("meeting.prepare").retrieves_knowledge is True
    assert get_capability("business.qa").max_facts == 50


async def test_object_qa_answer_cites_facts_not_history():
    history = [
        {"role": "user", "content": "先给我看下 ACME 的情况"},
        {"role": "assistant", "content": "已生成对象摘要"},
    ]
    final = await _run_graph("object.qa", dict(QUESTION_INPUT), history=history)
    assert "result" in final
    result = final["result"]
    assert result["read_only"] is True
    # 历史行 [H*] 不进引用白名单：即使模型引用也会被剥离（此处 Stub 未引用）
    assert all(c["ref_id"] in {"ACT-9", "TASK-9"} for c in result["citations"])


async def test_knowledge_qa_ignores_fabricated_knowledge_citation():
    true_fact = {"type": "knowledge", "id": "kb-real", "version": "v1", "text": "真实知识内容"}

    class FabricatingModel(StubChatModel):
        def _build_result(self, messages):
            import json

            from langchain_core.messages import AIMessage
            from langchain_core.outputs import ChatGeneration, ChatResult

            payload = {
                "summary": "归纳",
                "sections": [{"title": "事实汇总", "points": ["一点"]}],
                "citations": [
                    {"ref_id": "kb-real", "type": "knowledge", "version": "v1"},
                    {"ref_id": "kb-ghost", "type": "knowledge", "version": "v9"},
                ],
            }
            m = AIMessage(content=json.dumps(payload, ensure_ascii=False))
            return ChatResult(generations=[ChatGeneration(message=m)])

    final = await _run_graph(
        "knowledge.qa", {"question": "报销政策是什么？"},
        model=FabricatingModel(), knowledge_facts=[true_fact],
    )
    result = final["result"]
    assert {c["ref_id"] for c in result["citations"]} == {"kb-real"}
    assert result["citation_violations"] == ["kb-ghost"]


async def test_knowledge_qa_without_any_context_discloses_without_model():
    calls = {"count": 0}

    class CountingModel(StubChatModel):
        async def _agenerate(self, *args, **kwargs):
            calls["count"] += 1
            return await super()._agenerate(*args, **kwargs)

    final = await _run_graph(
        "knowledge.qa", {"question": "价格政策？"}, model=CountingModel(), knowledge_facts=[]
    )
    assert calls["count"] == 0, "无可用依据时不得调用模型"
    result = final["result"]
    assert result["citations"] == []
    assert result["missing"]
    assert result["knowledge_used"] == {"count": 0, "refs": []}


async def test_business_qa_rejects_over_limit_in_graph():
    facts = [{"type": "metric", "id": f"M-{index}", "text": f"指标 {index}"} for index in range(51)]
    final = await _run_graph(
        "business.qa",
        {"question": "本月成交？", "facts": facts, "window": {"from": "2026-09-01", "to": "2026-09-30"}},
    )
    assert "超过 50 条" in final.get("error", "")


async def test_meeting_prepare_question_derived_from_topic():
    final = await _run_graph(
        "meeting.prepare",
        {"meeting": {"topic": "ACME 续约评审", "at": "2026-10-08"}, "facts": FACTS},
        knowledge_facts=[{"type": "knowledge", "id": "kb-1", "version": "v1", "text": "评审材料"}],
    )
    assert "result" in final
    assert final["result"]["read_only"] is True


# ---------------------------------------------------------------- API + 真实库


def _dev_settings(db_url: str) -> Settings:
    return Settings(
        environment="dev",
        database_url=db_url,
        model={"provider": "stub"},
        auth={"service_secret": "dev-secret", "service_key_id": "crm-ai"},
        worker={
            "enabled": True,
            "concurrency": 2,
            "poll_interval_seconds": 0.05,
            "lease_seconds": 120,
            "backoff_base_seconds": 0.2,
        },
    )


@pytest_asyncio.fixture
async def qa_api(db_url: str):
    settings = _dev_settings(db_url)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai") as client:
            yield client, settings


def _signed(settings: Settings, method: str, path: str, body: bytes, user_id: str = "u1") -> dict[str, str]:
    return sign_request(
        secret=settings.auth.service_secret,
        key_id=settings.auth.service_key_id,
        method=method,
        path=path,
        body=body,
        operator=OperatorContext(user_id=user_id, user_name="测试用户"),
    )


async def _post(client, settings, path: str, payload: dict | None = None, user_id: str = "u1") -> httpx.Response:
    """签名说明：停用等端点为无 body 的 POST，payload 允许省略（按空对象签名）。"""
    import json

    body = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
    headers = _signed(settings, "POST", path, body, user_id)
    headers["Content-Type"] = "application/json"
    return await client.post(path, content=body, headers=headers)


async def _get(client, settings, path: str, user_id: str = "u1") -> httpx.Response:
    return await client.get(path, headers=_signed(settings, "GET", path, b"", user_id))


async def _wait_status(
    client, settings, run_id: str, statuses: set[str], timeout: float = 20, user_id: str = "u1"
) -> dict:
    """轮询 Run 状态；user_id 需与 Run 归属一致（跨用户查询会被 403 拒绝）。"""
    body: dict = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = (await _get(client, settings, f"/api/v1/runs/{run_id}", user_id=user_id)).json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"等待状态超时: {statuses}, 最后: {body}")


async def _sync_doc(client, settings, doc: dict, user_id: str = "u1") -> dict:
    response = await _post(client, settings, "/api/v1/knowledge/documents", doc, user_id=user_id)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.realpg
async def test_knowledge_sync_store_scope_and_version(qa_api, db_pool):
    from app.knowledge import search_authorized

    client, settings = qa_api
    await _sync_doc(client, settings, dict(KNOW_DOC))
    await _sync_doc(client, settings, {**KNOW_DOC, "version": "v3", "content": "续约 85 折。"})
    await _sync_doc(client, settings, {**KNOW_DOC, "knowledge_id": "kb-public", "content": "公司简介。", "scope": {"public": True}})
    await _sync_doc(client, settings, {**KNOW_DOC, "knowledge_id": "kb-other", "content": "他人知识。", "scope": {"users": ["someone"]}})

    async with db_pool.connection() as conn:
        mine = await search_authorized(conn, user_id="u1")
        mine_ids = {doc["knowledge_id"] for doc in mine}
        assert "kb-pricing" in mine_ids and "kb-public" in mine_ids
        assert "kb-other" not in mine_ids
        pricing = next(doc for doc in mine if doc["knowledge_id"] == "kb-pricing")
        assert pricing["version"] == "v3"

        other = await search_authorized(conn, user_id="u2")
        assert {doc["knowledge_id"] for doc in other} == {"kb-public"}

    # 停用后立即从可用检索消失（撤权立即生效）
    disabled = await _post(client, settings, "/api/v1/knowledge/documents/kb-pricing/disable")
    assert disabled.status_code == 200
    assert disabled.json()["disabled_versions"] >= 2
    async with db_pool.connection() as conn:
        after = await search_authorized(conn, user_id="u1")
        assert "kb-pricing" not in {doc["knowledge_id"] for doc in after}


@pytest.mark.realpg
async def test_knowledge_scope_rejects_empty(qa_api):
    client, settings = qa_api
    response = await _post(client, settings, "/api/v1/knowledge/documents", {**KNOW_DOC, "scope": {}})
    assert response.status_code == 422
    missing = await _post(client, settings, "/api/v1/knowledge/documents/unknown-doc/disable")
    assert missing.status_code == 404


@pytest.mark.realpg
async def test_knowledge_qa_end_to_end_cites_version(qa_api):
    client, settings = qa_api
    await _sync_doc(client, settings, dict(KNOW_DOC))

    created = (
        await _post(client, settings, "/api/v1/runs",
                    {"capability": "knowledge.qa", "input": {"question": "续约折扣是多少？"}})
    ).json()
    run = await _wait_status(client, settings, created["run_id"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("error")
    assert run["prompt_version"] == "knowledge-qa-prompt@1"
    assert run["graph_version"] == "analysis-qa@1"
    result = run["result"]
    assert result["read_only"] is True
    assert result["knowledge_used"]["count"] == 1
    assert {c["ref_id"] for c in result["citations"]} == {"kb-pricing"}
    assert next(c for c in result["citations"] if c["ref_id"] == "kb-pricing")["version"] == "v2"

    # 停用后新 Run 不再引用（撤权立即生效）
    await _post(client, settings, "/api/v1/knowledge/documents/kb-pricing/disable")
    created2 = (
        await _post(client, settings, "/api/v1/runs",
                    {"capability": "knowledge.qa", "input": {"question": "续约折扣是多少？"},
                     "idempotency_key": "kq2-" + uuid.uuid4().hex[:8]})
    ).json()
    run2 = await _wait_status(client, settings, created2["run_id"], {"succeeded", "failed"})
    assert run2["status"] == "succeeded"
    assert run2["result"]["citations"] == []
    assert run2["result"]["missing"]

    # 跨用户：无权用户检索不到该知识（隔离）
    created3 = (
        await _post(client, settings, "/api/v1/runs",
                    {"capability": "knowledge.qa", "input": {"question": "续约折扣是多少？"},
                     "idempotency_key": "kq3-" + uuid.uuid4().hex[:8]}, user_id="u2")
    ).json()
    run3 = await _wait_status(client, settings, created3["run_id"], {"succeeded", "failed"}, user_id="u2")
    assert run3["status"] == "succeeded"
    assert all(c["ref_id"] != "kb-pricing" for c in run3["result"]["citations"])
    assert run3["result"]["knowledge_used"] == {"count": 0, "refs": []}


@pytest.mark.realpg
async def test_object_qa_conversation_binding(qa_api):
    client, settings = qa_api
    # 无会话：422
    no_conv = await _post(client, settings, "/api/v1/runs",
                          {"capability": "object.qa", "input": dict(QUESTION_INPUT)})
    assert no_conv.status_code == 422

    # 建对象绑定会话
    import json as jsonlib

    raw = jsonlib.dumps({"subject_type": "customer", "subject_id": "C-9"}, ensure_ascii=False).encode("utf-8")
    conv = await client.post(
        "/api/v1/conversations", content=raw,
        headers={**_signed(settings, "POST", "/api/v1/conversations", raw), "Content-Type": "application/json"},
    )
    assert conv.status_code == 201, conv.text
    conv_id = conv.json()["conversation_id"]

    # 绑定不一致：422
    wrong = await _post(
        client, settings, "/api/v1/runs",
        {"capability": "object.qa",
         "input": {**dict(QUESTION_INPUT), "scope": {"subject_type": "customer", "subject_id": "C-OTHER"}},
         "conversation_id": conv_id, "idempotency_key": "oq-w-" + uuid.uuid4().hex[:8]},
    )
    assert wrong.status_code == 422

    # 一致：202 并成功；助手消息记录回答而非候选模板
    ok = await _post(
        client, settings, "/api/v1/runs",
        {"capability": "object.qa", "input": dict(QUESTION_INPUT),
         "conversation_id": conv_id, "idempotency_key": "oq-ok-" + uuid.uuid4().hex[:8]},
    )
    assert ok.status_code == 202, ok.text
    run = await _wait_status(client, settings, ok.json()["run_id"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("error")
    assert run["graph_version"] == "analysis-qa@1"

    msgs = await _get(client, settings, f"/api/v1/conversations/{conv_id}/messages")
    assert msgs.status_code == 200
    assistant = [m for m in msgs.json()["messages"] if m["role"] == "assistant"]
    assert assistant, "对象问答应落库助手回答"
    assert "候选" not in assistant[-1]["content"]


@pytest.mark.realpg
async def test_business_qa_window_and_limit(qa_api):
    client, settings = qa_api
    no_window = await _post(client, settings, "/api/v1/runs",
                            {"capability": "business.qa", "input": {"question": "x", "facts": FACTS}})
    assert no_window.status_code == 422

    too_many = await _post(client, settings, "/api/v1/runs",
                           {"capability": "business.qa",
                            "input": {"question": "x",
                                      "facts": [{"type": "metric", "id": f"M-{i}", "text": f"指标 {i}"} for i in range(51)],
                                      "window": {"from": "2026-09-01", "to": "2026-09-30"}}})
    assert too_many.status_code == 422

    ok = await _post(client, settings, "/api/v1/runs",
                     {"capability": "business.qa",
                      "input": {"question": "本月成交多少？", "facts": FACTS,
                                "window": {"from": "2026-09-01", "to": "2026-09-30"}}})
    assert ok.status_code == 202, ok.text
    run = await _wait_status(client, settings, ok.json()["run_id"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("error")
    assert run["prompt_version"] == "business-qa-prompt@1"


@pytest.mark.realpg
async def test_meeting_prepare_mixes_facts_and_knowledge(qa_api):
    client, settings = qa_api
    await _sync_doc(client, settings, dict(KNOW_DOC))

    created = (
        await _post(client, settings, "/api/v1/runs",
                    {"capability": "meeting.prepare",
                     "input": {"meeting": {"topic": "ACME 季度复盘", "at": "2026-10-08"}, "facts": FACTS}})
    ).json()
    run = await _wait_status(client, settings, created["run_id"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("error")
    result = run["result"]
    assert result["read_only"] is True
    assert result["knowledge_used"]["count"] == 1
    assert "kb-pricing" in {c["ref_id"] for c in result["citations"]}
    assert "ACT-9" in {c["ref_id"] for c in result["citations"]}

@pytest.mark.realpg
async def test_knowledge_search_ranks_by_question(qa_api, db_pool):
    """知识检索按问题相关度排序：问"报销"时不应拿"招聘"当依据。

    相关性只用于排序，不参与授权判定——无关但有权、且提问无命中时仍返回原序。
    """
    from app.knowledge import search_authorized

    client, settings = qa_api
    await _sync_doc(client, settings, {
        "knowledge_id": "kb-travel", "version": "v1", "title": "差旅报销标准",
        "content": "出差住宿每晚上限 500 元，需提交发票。", "scope": {"users": ["u1"]},
    })
    await _sync_doc(client, settings, {
        "knowledge_id": "kb-hiring", "version": "v1", "title": "招聘流程",
        "content": "简历筛选后进行两轮面试。", "scope": {"users": ["u1"]},
    })
    await _sync_doc(client, settings, {
        "knowledge_id": "kb-leave", "version": "v1", "title": "请假制度",
        "content": "年假需提前三个工作日申请。", "scope": {"users": ["u1"]},
    })

    async with db_pool.connection() as conn:
        ranked = await search_authorized(conn, user_id="u1", query="报销标准是多少？", limit=3)
        assert [d["knowledge_id"] for d in ranked][0] == "kb-travel"

        # 全部零命中时保持原序，不假装检索到相关文档
        none_hit = await search_authorized(conn, user_id="u1", query="zzzz 无关查询", limit=3)
        assert len(none_hit) == 3

        # 相关性不得绕过授权：u2 无权时仍检索不到
        others = await search_authorized(conn, user_id="u2", query="报销标准是多少？", limit=3)
        assert others == []


@pytest.mark.realpg
async def test_knowledge_search_ranks_by_question(qa_api, db_pool):
    """知识检索按问题相关度排序：问"报销"时不应拿"招聘"当依据。

    相关性只用于排序，不参与授权判定——无关但有权、且提问无命中时仍返回原序。
    """
    from app.knowledge import search_authorized

    client, settings = qa_api
    await _sync_doc(client, settings, {
        "knowledge_id": "kb-travel", "version": "v1", "title": "差旅报销标准",
        "content": "出差住宿每晚上限 500 元，需提交发票。", "scope": {"users": ["u1"]},
    })
    await _sync_doc(client, settings, {
        "knowledge_id": "kb-hiring", "version": "v1", "title": "招聘流程",
        "content": "简历筛选后进行两轮面试。", "scope": {"users": ["u1"]},
    })
    await _sync_doc(client, settings, {
        "knowledge_id": "kb-leave", "version": "v1", "title": "请假制度",
        "content": "年假需提前三个工作日申请。", "scope": {"users": ["u1"]},
    })

    async with db_pool.connection() as conn:
        ranked = await search_authorized(conn, user_id="u1", query="报销标准是多少？", limit=3)
        assert [d["knowledge_id"] for d in ranked][0] == "kb-travel"

        # 全部零命中时保持原序，不假装检索到相关文档
        none_hit = await search_authorized(conn, user_id="u1", query="zzzz 无关查询", limit=3)
        assert len(none_hit) == 3

        # 相关性不得绕过授权：u2 无权时仍检索不到
        others = await search_authorized(conn, user_id="u2", query="报销标准是多少？", limit=3)
        assert others == []
