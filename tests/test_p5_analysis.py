"""P5 只读分析能力测试：能力注册表、分析图守卫、引用完整性、预生成调度幂等。

覆盖需求 4.4 日报、4.5 对象摘要、4.10 今日任务、4.11 主管关注，以及
「只读不写正式字段」「事实由 CRM 装配」「引用可核」「两端不重复调度」约束。

分两层（与仓库既有约定一致）：
- 图逻辑用 MemorySaver 直接跑（不依赖 PostgreSQL）；
- API/持久化用真实 PostgreSQL + 内置 worker（pytest.mark.realpg）。
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
from app.capabilities import (
    CAPABILITIES,
    analysis_capabilities,
    pregen_capabilities,
    pregen_idempotency_key,
)
from app.config import Settings
from app.graphs.builder import compile_graph_for
from app.providers.stub import StubChatModel

FACTS = [
    {
        "type": "activity",
        "id": "ACT-1",
        "version": "3",
        "occurred_at": "2026-09-29",
        "text": "拜访 ACME，沟通续约事宜",
    },
    {"type": "task", "id": "TASK-7", "version": "1", "text": "准备续约报价单"},
]

DAILY_INPUT = {
    "facts": FACTS,
    "window": {"from": "2026-09-29", "to": "2026-09-29"},
    "beneficiary": {"user_id": "u1", "user_name": "张三"},
    "schedule_key": "u1:2026-09-29:daily",
    "request_ref": "crm-req-1",
}


async def _run_graph(capability: str, payload: dict, model=None, thread_id: str = "t1"):
    graph = compile_graph_for(capability, model or StubChatModel(), checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": thread_id}}
    return await graph.ainvoke({"capability": capability, "input_payload": payload}, config=config)


def test_capability_registry_contract():
    assert set(CAPABILITIES) >= {
        "communication.extract",
        "daily.draft",
        "object.summary",
        "today.summary",
        "manager.focus",
    }
    for name, spec in CAPABILITIES.items():
        assert spec.name == name
        assert spec.graph_version and spec.prompt_version
        if spec.kind == "analysis":
            assert spec.read_only is True, name
            assert not spec.requires_text, name
            if not spec.requires_facts:
                # 不装配 CRM facts 的分析能力，必须自己按授权检索知识
                # （需求 4.8 knowledge.qa：知识由 AI 授权索引提供，过滤先于内容进模型）
                assert spec.retrieves_knowledge, name
            assert spec.prompt_template, name
    # 仅日报底稿/今日任务/主管关注允许预生成（需求 4 结尾段）
    assert set(pregen_capabilities()) == {"daily.draft", "today.summary", "manager.focus"}
    assert "object.summary" in analysis_capabilities()


def test_pregen_idempotency_key_is_deterministic():
    first = pregen_idempotency_key(capability="daily.draft", schedule_key="u1:2026-09-29")
    second = pregen_idempotency_key(capability="daily.draft", schedule_key="u1:2026-09-29")
    other = pregen_idempotency_key(capability="today.summary", schedule_key="u1:2026-09-29")
    assert first == second
    assert first != other


async def test_daily_draft_produces_read_only_result():
    final = await _run_graph("daily.draft", DAILY_INPUT)
    result = final["result"]
    assert result["kind"] == "analysis"
    assert result["read_only"] is True
    assert result["capability"] == "daily.draft"
    assert result["facts_used"]["count"] == 2
    assert {c["ref_id"] for c in result["citations"]} == {"ACT-1", "TASK-7"}
    assert result["citation_violations"] == []
    assert "只读" in result["notes"]
    assert result["sections"][0]["points"]
    # 只读结果不得携带任何正式写入指令
    assert "candidates" not in result


async def test_fabricated_citation_is_stripped():
    class FabricatingModel(StubChatModel):
        def _build_result(self, messages):
            import json

            from langchain_core.messages import AIMessage
            from langchain_core.outputs import ChatGeneration, ChatResult

            payload = {
                "summary": "归纳",
                "sections": [{"title": "事实汇总", "points": ["一点"]}],
                "citations": [
                    {"ref_id": "ACT-1", "type": "activity", "version": "3"},
                    {"ref_id": "FAKE-999", "type": "task", "version": "1"},
                ],
            }
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=json.dumps(payload, ensure_ascii=False)))]
            )

    final = await _run_graph("object.summary", {**DAILY_INPUT, "scope": {"subject_type": "customer", "subject_id": "C-1"}}, model=FabricatingModel())
    result = final["result"]
    assert {c["ref_id"] for c in result["citations"]} == {"ACT-1"}
    assert result["citation_violations"] == ["FAKE-999"]


async def test_missing_facts_fails_before_model_call():
    calls = {"count": 0}

    class CountingModel(StubChatModel):
        async def _agenerate(self, *args, **kwargs):
            calls["count"] += 1
            return await super()._agenerate(*args, **kwargs)

    final = await _run_graph("daily.draft", {"window": {"from": "2026-09-29", "to": "2026-09-29"}}, model=CountingModel())
    assert final.get("error")
    assert "facts" in final["error"]
    assert calls["count"] == 0, "事实缺失时不得调用模型（不编造结果）"
    assert final.get("result") is None


async def test_pregen_requires_beneficiary_and_schedule_key():
    without_beneficiary = {k: v for k, v in DAILY_INPUT.items() if k != "beneficiary"}
    final = await _run_graph("today.summary", without_beneficiary)
    assert "受益人" in final["error"]

    without_schedule = {k: v for k, v in DAILY_INPUT.items() if k != "schedule_key"}
    final = await _run_graph("today.summary", without_schedule)
    assert "schedule_key" in final["error"]


async def test_facts_without_id_rejected():
    payload = {**DAILY_INPUT, "facts": [{"type": "task", "text": "无 id 的事实"}]}
    final = await _run_graph("daily.draft", payload)
    assert "id" in final["error"]


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
async def p5_api(db_url: str):
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


async def _post(client: httpx.AsyncClient, settings: Settings, path: str, payload: dict, user_id: str = "u1") -> httpx.Response:
    import json

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _signed(settings, "POST", path, body, user_id)
    headers["Content-Type"] = "application/json"
    return await client.post(path, content=body, headers=headers)


async def _get(client: httpx.AsyncClient, settings: Settings, path: str, user_id: str = "u1") -> httpx.Response:
    return await client.get(path, headers=_signed(settings, "GET", path, b"", user_id))


async def _wait_status(client, settings, run_id: str, statuses: set[str], timeout: float = 20) -> dict:
    body: dict = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = (await _get(client, settings, f"/api/v1/runs/{run_id}")).json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"等待状态超时: {statuses}, 最后: {body}")
@pytest.mark.realpg
async def test_daily_draft_run_end_to_end(p5_api):
    client, settings = p5_api
    response = await _post(client, settings, "/api/v1/runs", {"capability": "daily.draft", "input": DAILY_INPUT})
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["read_only"] is True
    assert created["pregen"] is True
    assert created["idempotency_source"] == "pregen_schedule_key"

    run = await _wait_status(client, settings, created["run_id"], {"succeeded", "failed"})
    assert run["status"] == "succeeded", run.get("error")
    assert run["prompt_version"] == "daily-draft-prompt@1"
    assert run["graph_version"] == "analysis@1"
    result = run["result"]
    assert result["read_only"] is True
    assert {c["ref_id"] for c in result["citations"]} == {"ACT-1", "TASK-7"}


@pytest.mark.realpg
async def test_pregen_duplicate_schedule_key_returns_same_run(p5_api):
    client, settings = p5_api
    body = {"capability": "today.summary", "input": DAILY_INPUT}
    first = (await _post(client, settings, "/api/v1/runs", body)).json()
    await _wait_status(client, settings, first["run_id"], {"succeeded", "failed"})

    second_response = await _post(client, settings, "/api/v1/runs", body)
    second = second_response.json()
    assert second_response.status_code == 202
    assert second["run_id"] == first["run_id"]
    assert second["idempotent_replay"] is True
    # 重复调度不得把已完成的 Run 打回队列（两端不重复调度同一业务任务）
    assert second["status"] == "succeeded"


@pytest.mark.realpg
async def test_analysis_input_validation(p5_api):
    client, settings = p5_api

    no_facts = await _post(client, settings, "/api/v1/runs", {
        "capability": "daily.draft",
        "input": {
            "window": {"from": "2026-09-29", "to": "2026-09-29"},
            "beneficiary": {"user_id": "u1"},
            "schedule_key": "u1:2026-09-29",
        },
    })
    assert no_facts.status_code == 422
    assert no_facts.json()["error"]["code"] == "VALIDATION_FAILED"

    no_schedule = await _post(client, settings, "/api/v1/runs", {
        "capability": "manager.focus",
        "input": {
            "facts": FACTS,
            "window": {"from": "2026-09-29", "to": "2026-09-29"},
            "beneficiary": {"user_id": "u1"},
        },
    })
    assert no_schedule.status_code == 422
    assert "schedule_key" in no_schedule.json()["error"]["message"]

    unknown = await _post(client, settings, "/api/v1/runs", {
        "capability": "report.draft",
        "input": {"text": "x"},
        "idempotency_key": "unknown-" + uuid.uuid4().hex[:8],
    })
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "CAPABILITY_UNKNOWN"

    short_key = await _post(client, settings, "/api/v1/runs", {
        "capability": "communication.extract",
        "input": {"text": "客户：ACME"},
        "idempotency_key": "short",
    })
    assert short_key.status_code == 422


@pytest.mark.realpg
async def test_extract_still_requires_text(p5_api):
    client, settings = p5_api
    empty = await _post(client, settings, "/api/v1/runs", {
        "capability": "communication.extract",
        "input": {"text": "  "},
        "idempotency_key": "empty-" + uuid.uuid4().hex[:8],
    })
    assert empty.status_code == 422
    assert "text" in empty.json()["error"]["message"]


@pytest.mark.realpg
async def test_analysis_run_is_owner_scoped(p5_api):
    client, settings = p5_api
    created = (await _post(client, settings, "/api/v1/runs", {
        "capability": "object.summary",
        "input": {**DAILY_INPUT, "scope": {"subject_type": "customer", "subject_id": "C-1"}},
        "idempotency_key": "owner-" + uuid.uuid4().hex[:8],
    })).json()
    await _wait_status(client, settings, created["run_id"], {"succeeded", "failed"})

    other = await _get(client, settings, f"/api/v1/runs/{created['run_id']}", user_id="u2")
    assert other.status_code == 403

    owner = await _get(client, settings, f"/api/v1/runs/{created['run_id']}", user_id="u1")
    assert owner.status_code == 200
    assert owner.json()["result"]["read_only"] is True