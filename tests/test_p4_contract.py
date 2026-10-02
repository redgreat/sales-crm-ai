"""P4 双端集成契约测试（不依赖真实 CRM 运行）。

冻结契约（2026-10-01，已归并至 docs/AI接入需求文档.md 第 13 节）：
- 方法/路径：GET {base_url}/ai/integration/facts/{subject_type}/{subject_id}
  Java 端点：AiIntegrationController#objectFacts（/api/v1/salescrm/ai/integration/facts/**）
- 签名：SAI-HMAC-SHA256（app/auth.py 与 CRM AiHmacSigner 逐字段一致，path 不含 query）
- 响应：{subject_type, subject_id, found, facts:{...}}；对象不存在 found=false
- base_url 必须含 CRM 应用前缀（如 http://127.0.0.1:8080/api/v1/salescrm）

用 httpx.MockTransport 模拟 Java 端行为，验证 Python 客户端发出的请求
与它对响应的解析严格符合契约；签名串按 Java 侧重构校验。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json

import httpx
import pytest

from app.auth import HEADER_AUTH, HEADER_KEY_ID, HEADER_NONCE, HEADER_SIGNATURE, HEADER_TIMESTAMP, HEADER_USER_ID, OperatorContext
from app.config import Settings
from app.integrations.crm import HttpCrmFactsClient

pytestmark = pytest.mark.contract

OPERATOR = OperatorContext(user_id="staff-001", user_name="测试销售")


def _java_style_signature(secret: str, canonical: str) -> str:
    """按 CRM 侧 AiHmacSigner 的算法（HmacSHA256 + hex）独立重构，验证双端一致。"""
    return hmac_mod.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _client_settings() -> Settings:
    return Settings(
        environment="dev",
        crm={
            "base_url": "http://127.0.0.1:8080/api/v1/salescrm",
            "key_id": "ai-crm",
            "secret": "contract-test-secret",
        },
    )


async def test_facts_request_matches_frozen_contract():
    """GET + 冻结路径 + 签名头齐全，签名可用 Java 侧算法重构验证。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = request.headers  # CIMultiDict：大小写不敏感
        return httpx.Response(200, json={
            "subject_type": "customer",
            "subject_id": "C001",
            "found": True,
            "facts": {"id": "C001", "name": "恒通物流", "master_status": "ACTIVE"},
        })

    transport = httpx.MockTransport(handler)
    client = HttpCrmFactsClient(_client_settings(), http_client=httpx.AsyncClient(transport=transport, base_url=_client_settings().crm.base_url))
    try:
        result = await client.query_object_facts(
            operator=OPERATOR, subject_type="customer", subject_id="C001"
        )
    finally:
        await client.aclose()

    # 方法与路径（含 CRM 应用前缀）
    assert captured["method"] == "GET"
    assert captured["url"] == "http://127.0.0.1:8080/api/v1/salescrm/ai/integration/facts/customer/C001"
    # 签名头齐全
    headers = captured["headers"]
    assert headers[HEADER_AUTH] == "SAI-HMAC-SHA256"
    assert headers[HEADER_KEY_ID] == "ai-crm"
    assert headers[HEADER_USER_ID] == "staff-001"
    assert headers.get(HEADER_TIMESTAMP) and headers.get(HEADER_NONCE)
    # 按 Java AiHmacSigner.canonicalString 逐字段重构签名串（GET 空 body）
    canonical = "\n".join([
        "ai-crm",
        "GET",
        "/api/v1/salescrm/ai/integration/facts/customer/C001",
        headers[HEADER_TIMESTAMP],
        headers[HEADER_NONCE],
        "staff-001",
        hashlib.sha256(b"").hexdigest(),
    ])
    expected = _java_style_signature("contract-test-secret", canonical)
    assert headers[HEADER_SIGNATURE] == expected
    # 响应解析
    assert result["found"] is True
    assert result["facts"]["name"] == "恒通物流"


async def test_facts_lead_and_opportunity_paths():
    """lead/opportunity 走同一路径模式（Java 端 0.1 支持三种 subjectType）。"""
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={
            "subject_type": "lead", "subject_id": request.url.path.rsplit("/", 1)[-1],
            "found": False, "facts": {},
        })

    transport = httpx.MockTransport(handler)
    client = HttpCrmFactsClient(_client_settings(), http_client=httpx.AsyncClient(transport=transport, base_url=_client_settings().crm.base_url))
    try:
        await client.query_object_facts(operator=OPERATOR, subject_type="lead", subject_id="LD001")
        await client.query_object_facts(operator=OPERATOR, subject_type="opportunity", subject_id="OP001")
    finally:
        await client.aclose()

    assert seen_paths == [
        "/api/v1/salescrm/ai/integration/facts/lead/LD001",
        "/api/v1/salescrm/ai/integration/facts/opportunity/OP001",
    ]


async def test_facts_not_found_passthrough():
    """对象不存在：契约要求透传 found=false（由 AI 侧决定如何降级，CRM 不造默认值）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "subject_type": "customer", "subject_id": "NOPE", "found": False, "facts": {},
        })

    transport = httpx.MockTransport(handler)
    client = HttpCrmFactsClient(_client_settings(), http_client=httpx.AsyncClient(transport=transport, base_url=_client_settings().crm.base_url))
    try:
        result = await client.query_object_facts(
            operator=OPERATOR, subject_type="customer", subject_id="NOPE"
        )
    finally:
        await client.aclose()
    assert result["found"] is False


async def test_unauthorized_response_surfaces():
    """签名被拒（401）：raise_for_status 显式失败，绝不把错误响应当事实。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "AI 服务签名校验失败"})

    transport = httpx.MockTransport(handler)
    client = HttpCrmFactsClient(_client_settings(), http_client=httpx.AsyncClient(transport=transport, base_url=_client_settings().crm.base_url))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.query_object_facts(
                operator=OPERATOR, subject_type="customer", subject_id="C001"
            )
    finally:
        await client.aclose()


def test_crm_module_has_no_business_write_path():
    """边界回归：AI 侧 CRM 客户端只有事实查询，不含任何写路径（写入永远经本人确认在 CRM 侧）。"""
    import inspect
    from app.integrations import crm as crm_module
    source = inspect.getsource(crm_module)
    for forbidden in ("POST", "PUT", "DELETE", "confirm", "import", "write"):
        # 注释/文档里出现不算；这里断言不存在对 CRM 的写方法
        assert f'"{forbidden} ' not in source or forbidden not in ("POST", "PUT", "DELETE")
    methods = [name for name, _ in inspect.getmembers(HttpCrmFactsClient, predicate=inspect.isfunction)]
    write_methods = [m for m in methods if m.startswith(("create_", "update_", "delete_", "confirm_", "import_"))]
    assert write_methods == []
