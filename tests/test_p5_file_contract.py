"""P5 文件边界契约测试（不依赖真实 CRM 运行，无 OSS/供应商调用）。

冻结契约（2026-10-07，见 docs/AI接入需求文档.md 12.5）：
- 方法/路径：GET {base_url}/ai/integration/files/{fileId}（元数据+访问方式）
  与 GET {base_url}/ai/integration/files/{fileId}/content（仅 local 存储模式取字节）
  Java 端点：AiFileIntegrationController（/api/v1/salescrm/ai/integration/files/**，HMAC）
- 响应：{found, file:{id,file_name,content_type,file_kind,size_bytes,source_version,status,...},
  access:{type: signed_url|inline|none, url?, expires_in_seconds?, content_path?}}
- 非本人/不存在/已删除归一 found=false；撤权（本人）found=true + access.type=none
- AI 持久化文件标识而非签名 URL；URL 过期重新调用获取（需求 12.3）

用 httpx.MockTransport 模拟 Java 端行为；签名串按 Java 侧重构校验。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod

import httpx
import pytest

from app.auth import (
    HEADER_AUTH,
    HEADER_KEY_ID,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HEADER_USER_ID,
    OperatorContext,
)
from app.config import Settings
from app.integrations.crm import HttpCrmFactsClient, StubCrmFactsClient

pytestmark = pytest.mark.contract

OPERATOR = OperatorContext(user_id="ST0000000001", user_name="测试销售")
FILE_ID = "RS0000000001"


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


def _client(transport: httpx.MockTransport) -> HttpCrmFactsClient:
    settings = _client_settings()
    return HttpCrmFactsClient(
        settings,
        http_client=httpx.AsyncClient(transport=transport, base_url=settings.crm.base_url),
    )


async def test_file_access_request_matches_frozen_contract():
    """GET + 冻结路径 + 签名头齐全，签名可用 Java 侧算法重构验证；响应逐字段解析。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(200, json={
            "found": True,
            "file": {
                "id": FILE_ID,
                "file_name": "拜访录音.m4a",
                "content_type": "audio/mp4",
                "file_kind": "audio",
                "size_bytes": 1024,
                "source_version": 1,
                "status": "ACTIVE",
                "biz_type": "",
                "biz_id": "",
                "created_at": "2026-10-07T10:00:00+08:00",
            },
            "access": {
                "type": "signed_url",
                "url": "https://bucket.oss-cn-hangzhou.aliyuncs.com/resource/202610/x.m4a?sig=1",
                "expires_in_seconds": 3600,
            },
        })

    client = _client(httpx.MockTransport(handler))
    try:
        result = await client.get_integration_file(operator=OPERATOR, file_id=FILE_ID)
    finally:
        await client.aclose()

    assert captured["method"] == "GET"
    assert captured["url"] == f"http://127.0.0.1:8080/api/v1/salescrm/ai/integration/files/{FILE_ID}"
    headers = captured["headers"]
    assert headers[HEADER_AUTH] == "SAI-HMAC-SHA256"
    assert headers[HEADER_KEY_ID] == "ai-crm"
    assert headers[HEADER_USER_ID] == "ST0000000001"
    assert headers.get(HEADER_TIMESTAMP) and headers.get(HEADER_NONCE)
    canonical = "\n".join([
        "ai-crm",
        "GET",
        f"/api/v1/salescrm/ai/integration/files/{FILE_ID}",
        headers[HEADER_TIMESTAMP],
        headers[HEADER_NONCE],
        "ST0000000001",
        hashlib.sha256(b"").hexdigest(),
    ])
    assert headers[HEADER_SIGNATURE] == _java_style_signature("contract-test-secret", canonical)

    assert result["found"] is True
    assert result["file"]["file_kind"] == "audio"
    assert result["file"]["source_version"] == 1
    assert result["access"]["type"] == "signed_url"
    assert result["access"]["url"].startswith("https://")


async def test_file_access_missing_normalized_to_found_false():
    """非本人/不存在：CRM 归一 found=false（200），AI 透传不造默认值。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"found": False, "file": None})

    client = _client(httpx.MockTransport(handler))
    try:
        result = await client.get_integration_file(operator=OPERATOR, file_id="RS9999999999")
    finally:
        await client.aclose()
    assert result["found"] is False
    assert result["file"] is None


async def test_file_access_revoked_surfaces_access_none():
    """撤权（本人可见）：found=true + status=REVOKED + access.type=none——AI 不得再使用。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "found": True,
            "file": {"id": FILE_ID, "file_kind": "audio", "status": "REVOKED", "source_version": 1},
            "access": {"type": "none"},
        })

    client = _client(httpx.MockTransport(handler))
    try:
        result = await client.get_integration_file(operator=OPERATOR, file_id=FILE_ID)
    finally:
        await client.aclose()
    assert result["file"]["status"] == "REVOKED"
    assert result["access"]["type"] == "none"


async def test_file_inline_download_returns_bytes():
    """local 存储模式：/content 端点取字节（OCR/文本解析走二进制，不经 OSS URL）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"PDF-ish-bytes",
            headers={"Content-Type": "application/pdf"},
        )

    client = _client(httpx.MockTransport(handler))
    try:
        data = await client.download_integration_file(operator=OPERATOR, file_id=FILE_ID)
    finally:
        await client.aclose()
    assert data == b"PDF-ish-bytes"


async def test_file_download_oss_mode_rejected_surfaces():
    """oss 模式下 CRM 拒绝经 CRM 取字节（400）：AI 侧必须显式失败，不静默当空文件。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "oss 存储模式请使用签名 URL"}})

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.download_integration_file(operator=OPERATOR, file_id=FILE_ID)
    finally:
        await client.aclose()


async def test_file_id_injection_guard():
    """file_id 只允许白名单字符：路径注入/查询串在客户端就被拒绝，不发请求。"""
    client = _client(httpx.MockTransport(lambda request: pytest.fail("不应发出请求")))
    try:
        for bad in ("../secret", "RS1?x=1", "RS1/../../etc", "", "  "):
            with pytest.raises(ValueError):
                await client.get_integration_file(operator=OPERATOR, file_id=bad)
            with pytest.raises(ValueError):
                await client.download_integration_file(operator=OPERATOR, file_id=bad)
    finally:
        await client.aclose()


async def test_unauthorized_response_surfaces():
    """签名被拒（401）：显式失败，绝不把错误响应当附件。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "AI 服务签名校验失败"})

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_integration_file(operator=OPERATOR, file_id=FILE_ID)
    finally:
        await client.aclose()


async def test_stub_client_serves_metadata_and_bytes():
    """联调 Stub：登记过的文件返回元数据+inline 访问与字节；未登记 found=false。"""
    stub = StubCrmFactsClient(
        files={FILE_ID: {"id": FILE_ID, "file_kind": "image", "source_version": 1, "status": "ACTIVE"}},
        file_bytes={FILE_ID: b"png-bytes"},
    )
    meta = await stub.get_integration_file(operator=OPERATOR, file_id=FILE_ID)
    assert meta["found"] is True
    assert meta["file"]["file_kind"] == "image"
    assert meta["access"]["content_path"].endswith(f"/files/{FILE_ID}/content")
    assert await stub.download_integration_file(operator=OPERATOR, file_id=FILE_ID) == b"png-bytes"

    missing = await stub.get_integration_file(operator=OPERATOR, file_id="RS0000000002")
    assert missing == {"found": False, "file": None}

    with pytest.raises(ValueError):
        await stub.get_integration_file(operator=OPERATOR, file_id="bad/id")
