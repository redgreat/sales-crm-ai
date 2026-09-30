"""P5 需求 4.14：普通文件解析适配层。

不依赖外部服务与凭据。覆盖：纯文本类型解析、编码回退、CSV/JSON 渲染、
不支持类型与二进制显式报错（不留假结果）、截断标记、批量逐项失败原因。
"""
from __future__ import annotations

import pytest

from app.integrations.files import (
    SUPPORTED_TYPES,
    FileError,
    detect_type,
    parse_bytes,
    parse_many,
)


def test_detect_type_from_name_and_mime():
    assert detect_type(name="report.TXT") == ".txt"
    assert detect_type(name="notes.md") == ".md"
    assert detect_type(name="data.csv") == ".csv"
    # 无文件名时退回 MIME
    assert detect_type(name="", content_type="application/json") == ".json"
    assert detect_type(name="", content_type="text/plain; charset=utf-8") == ".txt"
    # 不支持的类型返回空串，由调用方显式报错
    assert detect_type(name="contract.pdf") == ""
    assert detect_type(name="sheet.xlsx") == ""


def test_parse_plain_text():
    parsed = parse_bytes(data="拜访 ACME，讨论续约价格。".encode("utf-8"), name="note.txt")
    assert parsed.text == "拜访 ACME，讨论续约价格。"
    assert parsed.file_type == ".txt"
    assert parsed.encoding == "utf-8"
    assert parsed.truncated is False


def test_parse_gb18030_fallback():
    data = "客户名称：测试公司".encode("gb18030")
    parsed = parse_bytes(data=data, name="note.txt")
    assert parsed.encoding == "gb18030"
    assert "测试公司" in parsed.text


def test_parse_csv_uses_readable_separator():
    parsed = parse_bytes(
        data="客户,阶段,金额\nACME,续约,50万\n".encode("utf-8"), name="list.csv"
    )
    assert "客户 | 阶段 | 金额" in parsed.text
    assert "ACME | 续约 | 50万" in parsed.text


def test_parse_json_compacts():
    parsed = parse_bytes(data='{"a": 1, "b": [2, 3]}'.encode("utf-8"), name="x.json")
    assert '"a": 1' in parsed.text
    assert '"b": [2, 3]' in parsed.text


def test_invalid_json_rejected():
    with pytest.raises(FileError, match="JSON 解析失败"):
        parse_bytes(data="{not json".encode("utf-8"), name="x.json")


def test_unsupported_type_rejected_without_fake_result():
    """docx/pdf/xlsx 必须显式报错——不能返回空文本假装解析成功。"""
    for name in ("a.pdf", "b.docx", "c.xlsx", "d.bin"):
        with pytest.raises(FileError, match="不支持的文件类型"):
            parse_bytes(data=b"whatever", name=name)


def test_binary_rejected():
    with pytest.raises(FileError, match="二进制"):
        parse_bytes(data=b"abc\x00\x01\x02", name="a.txt")


def test_empty_file_rejected():
    with pytest.raises(FileError, match="内容为空"):
        parse_bytes(data=b"", name="a.txt")


def test_undecodable_rejected():
    # 不含 NUL：确保走的是编码分支而不是二进制分支
    with pytest.raises(FileError, match="编码无法识别"):
        parse_bytes(data=b"\xff\xfe\x81\x81", name="a.txt")


def test_truncation_flagged():
    payload = "x" * 100
    parsed = parse_bytes(data=payload.encode("utf-8"), name="big.txt", max_bytes=10)
    assert parsed.truncated is True
    assert len(parsed.text) == 10
    assert "已截断" in parsed.summary()


def test_blank_only_file_rejected():
    with pytest.raises(FileError, match="没有可用文本"):
        parse_bytes(data=b"   \n\n  ", name="a.txt")


def test_parse_many_reports_per_file_failures():
    """批量不因单个失败而整批失败——逐项给出失败原因。"""
    parsed, failed = parse_many(
        [
            {"name": "ok.txt", "data": "内容".encode("utf-8")},
            {"name": "bad.pdf", "data": b"x"},
            {"name": "missing", "data": None},
        ]
    )
    assert [p.source_name for p in parsed] == ["ok.txt"]
    assert {f["name"] for f in failed} == {"bad.pdf", "missing"}
    assert all(f["reason"] for f in failed)


def test_supported_types_is_closed_set():
    """.pdf/.docx 等绝不能出现在支持集合里（避免能力被误认为已具备）。"""
    assert ".pdf" not in SUPPORTED_TYPES
    assert ".docx" not in SUPPORTED_TYPES
    assert ".xlsx" not in SUPPORTED_TYPES


# ------------------------------------------------------------------ 端点


async def test_parse_endpoint_returns_text_and_failures():
    """端点：成功给文本，失败给逐项原因，且不落库不调模型。"""
    import base64

    import httpx

    from app.api.main import create_app
    from app.config import Settings

    settings = Settings(
        environment="test",
        database_url="postgresql://postgres@127.0.0.1:1/x",
        auth={"service_key_id": "crm-ai", "service_secret": "test-secret"},
    )
    app = create_app(settings)
    app.state.settings = settings  # 不走 lifespan，手动挂上认证所需配置

    def b64(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/files/parse",
            json={
                "files": [
                    {"name": "note.txt", "data_base64": b64("拜访 ACME，讨论续约。")},
                    {"name": "contract.pdf", "data_base64": b64("x")},
                    {"name": "bad.txt", "data_base64": "!!!not-base64!!!"},
                ]
            },
            headers={"x-sai-user-id": "u1", "x-sai-user-name": "tester"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["parsed"][0]["text"] == "拜访 ACME，讨论续约。"
    reasons = {f["name"]: f["reason"] for f in body["failed"]}
    assert "不支持的文件类型" in reasons["contract.pdf"]
    assert "base64" in reasons["bad.txt"]


async def test_parse_endpoint_rejects_empty_payload():
    import httpx

    from app.api.main import create_app
    from app.config import Settings

    settings = Settings(
        environment="test",
        database_url="postgresql://postgres@127.0.0.1:1/x",
        auth={"service_key_id": "crm-ai", "service_secret": "test-secret"},
    )
    app = create_app(settings)
    app.state.settings = settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/files/parse",
            json={"files": []},
            headers={"x-sai-user-id": "u1", "x-sai-user-name": "tester"},
        )
    assert response.status_code == 422


# ------------------------------------------------------------------ 端点


async def test_parse_endpoint_returns_text_and_failures():
    """端点：成功给文本，失败给逐项原因，且不落库不调模型。"""
    import base64

    import httpx

    from app.api.main import create_app
    from app.config import Settings

    settings = Settings(
        environment="test",
        database_url="postgresql://postgres@127.0.0.1:1/x",
        auth={"service_key_id": "crm-ai", "service_secret": "test-secret"},
    )
    app = create_app(settings)
    app.state.settings = settings  # 不走 lifespan，手动挂上认证所需配置

    def b64(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/files/parse",
            json={
                "files": [
                    {"name": "note.txt", "data_base64": b64("拜访 ACME，讨论续约。")},
                    {"name": "contract.pdf", "data_base64": b64("x")},
                    {"name": "bad.txt", "data_base64": "!!!not-base64!!!"},
                ]
            },
            headers={"x-sai-user-id": "u1", "x-sai-user-name": "tester"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["parsed"][0]["text"] == "拜访 ACME，讨论续约。"
    reasons = {f["name"]: f["reason"] for f in body["failed"]}
    assert "不支持的文件类型" in reasons["contract.pdf"]
    assert "base64" in reasons["bad.txt"]


async def test_parse_endpoint_rejects_empty_payload():
    import httpx

    from app.api.main import create_app
    from app.config import Settings

    settings = Settings(
        environment="test",
        database_url="postgresql://postgres@127.0.0.1:1/x",
        auth={"service_key_id": "crm-ai", "service_secret": "test-secret"},
    )
    app = create_app(settings)
    app.state.settings = settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/files/parse",
            json={"files": []},
            headers={"x-sai-user-id": "u1", "x-sai-user-name": "tester"},
        )
    assert response.status_code == 422
