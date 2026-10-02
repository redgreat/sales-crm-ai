"""M20 助手建档草稿测试（产品版 V2.6 一期确认的 4 项新增类能力）。

产品红线（M20/M19，不做放宽）：
- 确认前不落库：AI 产物只是草稿，丢弃无痕；正式写入由 CRM 在本人确认后完成；
- 先匹配再新增：同名档案先提示已存在（查重在 CRM 导入侧，AI 不理解组织数据）；
- 创建类草稿必须补齐必填字段才能确认：缺什么追问什么（图 interrupt）；
- 每次写入与查询均标记来源（来源=助手）。

覆盖：注册表契约、能力目录透出、缺参追问-补参-草稿完整、一步到位、
丢弃无痕（AI 侧无任何写入路径）。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import pytest_asyncio

from app.api.main import create_app
from app.auth import OperatorContext, sign_request
from app.capabilities import DRAFT_GRAPH_VERSION, get_capability
from app.config import Settings

pytestmark = pytest.mark.realpg

DRAFT_CAPABILITIES = ("customer.draft", "contact.draft", "lead.draft", "opportunity.draft")


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
async def dev_api(db_url: str):
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
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _signed(settings, "POST", path, body, user_id)
    headers["Content-Type"] = "application/json"
    return await client.post(path, content=body, headers=headers)


async def _get(client: httpx.AsyncClient, settings: Settings, path: str, user_id: str = "u1") -> httpx.Response:
    return await client.get(path, headers=_signed(settings, "GET", path, b"", user_id))


async def _wait_status(client, settings, run_id: str, statuses: set[str], timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await _get(client, settings, f"/api/v1/runs/{run_id}")
        body = response.json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"等待 {run_id} 进入 {statuses} 超时")


# ---------------------------------------------------------------------------
# 注册表与能力目录契约
# ---------------------------------------------------------------------------


def test_draft_capabilities_registered():
    """4 项新增类能力入注册表：draft 图、草稿类型、必填追问、非预生成。"""
    for name in DRAFT_CAPABILITIES:
        spec = get_capability(name)
        assert spec is not None, name
        assert spec.graph_version == DRAFT_GRAPH_VERSION
        assert spec.draft_type in {"customer", "contact", "lead", "opportunity"}
        assert spec.requires_text and not spec.requires_facts
        assert not spec.pregen
        # AI 侧只产草稿：正式写入在 CRM（能力目录的 read_only 标识对写入端语义为「AI 不写」）
        assert spec.read_only
    # 必填字段集与 CRM DTO 对齐（缺什么追问什么）
    assert get_capability("customer.draft").draft_type == "customer"
    assert get_capability("contact.draft").draft_type == "contact"


async def test_capability_directory_exposes_drafts(dev_api):
    """GET /api/v1/capabilities 透出 4 项能力（前端入口契约由注册表渲染，M-12）。"""
    client, settings = dev_api
    response = await _get(client, settings, "/api/v1/capabilities")
    assert response.status_code == 200
    body = response.json()
    names = {c["capability"] for c in body["capabilities"]}
    assert set(DRAFT_CAPABILITIES) <= names


# ---------------------------------------------------------------------------
# 图行为：缺参追问 → 补参 → 草稿完整
# ---------------------------------------------------------------------------


async def test_customer_draft_asks_missing_and_completes(dev_api):
    """新增客户：缺客户类型 → 追问 → 补齐 → 草稿完整（确认前不落库，产物只有草稿）。"""
    client, settings = dev_api
    created = await _post(client, settings, "/api/v1/runs", {
        "capability": "customer.draft",
        "input": {"text": "新增客户：上海远大物流有限公司，行业：仓储物流"},
    })
    assert created.status_code == 202
    run_id = created.json()["run_id"]

    waiting = await _wait_status(client, settings, run_id, {"waiting_input"})
    pending = await _get(client, settings, f"/api/v1/runs/{run_id}/pending")
    questions = pending.json()["questions"]
    fields_asked = {q["field"] for q in questions}
    assert {"customer_type", "region", "biz_line"} <= fields_asked  # 缺什么追问什么
    state_version = pending.json()["state_version"]

    resumed = await _post(client, settings, f"/api/v1/runs/{run_id}/resume",
                          {"values": {"fields": {"customer_type": "ORG", "region": "华东", "biz_line": "车电业务线"}},
                           "state_version": state_version})
    assert resumed.status_code == 202

    final = await _wait_status(client, settings, run_id, {"succeeded"})
    draft = final["result"]["draft"]
    assert draft["draft_type"] == "customer"
    assert draft["fields"]["name"] == "上海远大物流有限公司"
    assert draft["fields"]["customer_type"] == "ORG"
    assert draft["fields"]["region"] == "华东"
    assert draft["fields"]["biz_line"] == "车电业务线"
    assert draft["fields"]["industry"] == "仓储物流"
    # 来源标记（M20 红线：每次写入标记来源=助手）
    assert final["result"]["references"]["source"] == "assistant.draft"


async def test_lead_draft_completes_without_followup(dev_api):
    """登记线索：必填齐备 → 不等待直接出草稿。"""
    client, settings = dev_api
    created = await _post(client, settings, "/api/v1/runs", {
        "capability": "lead.draft",
        "input": {"text": "登记线索：宁波袁老板想了解仓储方案，联系人：袁老板，电话：13998765432"},
    })
    run_id = created.json()["run_id"]
    final = await _wait_status(client, settings, run_id, {"succeeded"})
    draft = final["result"]["draft"]
    assert draft["draft_type"] == "lead"
    assert "宁波袁老板" in draft["fields"]["raw_content"]
    assert draft["fields"]["mobile"] == "13998765432"


async def test_contact_draft_requires_customer_and_name(dev_api):
    """新增联系人：所属客户与姓名都缺 → 一次追问两字段（缺什么追问什么）。"""
    client, settings = dev_api
    created = await _post(client, settings, "/api/v1/runs", {
        "capability": "contact.draft",
        "input": {"text": "记一下联系人，职务是采购总监"},
    })
    run_id = created.json()["run_id"]
    await _wait_status(client, settings, run_id, {"waiting_input"})
    pending = await _get(client, settings, f"/api/v1/runs/{run_id}/pending")
    fields_asked = {q["field"] for q in pending.json()["questions"]}
    assert {"customer_name", "contact_name"} <= fields_asked


async def test_draft_discard_leaves_no_business_write(dev_api):
    """丢弃无痕：草稿 Run 结束后 AI 侧没有任何对外写入路径（导入/确认在 CRM 侧显式发起）。"""
    import inspect

    from app.graphs import draft as draft_graph
    source = inspect.getsource(draft_graph)
    # 图内不得出现对 CRM 的调用（写入/HTTP 客户端）
    for forbidden in ("httpx", "HttpCrmFactsClient", "requests.", "post(", "put(", "patch("):
        assert forbidden not in source, forbidden
    client, settings = dev_api
    created = await _post(client, settings, "/api/v1/runs", {
        "capability": "customer.draft",
        "input": {"text": "新增客户：测试丢弃公司，客户类型：ORG"},
    })
    run_id = created.json()["run_id"]
    final = await _wait_status(client, settings, run_id, {"succeeded"})
    # 产物只是草稿数据，没有 formal id / 候选导入痕迹
    assert final["result"]["draft"]["fields"]["name"] == "测试丢弃公司"
    assert "formal" not in json.dumps(final["result"])
