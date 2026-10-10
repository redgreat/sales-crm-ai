"""连接契约（存 PG）：CRUD、分类互斥、运行期映射一致性、凭据只写不回显。

后台的价值在于「库里存多套预设、只有启用的那条生效」，所以这里同时校验
API 返回值与生效 Settings 的组装结果，防止界面与运行期出现两套真相。
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from app.admin import repo
from app.admin.effective import merge_records
from app.admin.overview import build_overview
from app.api.routes import auth as auth_routes
from app.api.routes import settings as settings_routes
from app.config import Settings
from app.errors import ApiError

pytestmark = pytest.mark.realpg

PASSWORD = "admin-pass-12345"


def _settings() -> Settings:
    return Settings(
        environment="dev",
        auth={"service_secret": "conn-unit-secret"},
        api={"playground": {"enabled": True}},
        model={"provider": "stub"},
    )


async def _client(pool: AsyncConnectionPool) -> httpx.AsyncClient:
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _handler(request, exc):  # noqa: ANN001, ARG001
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.state.settings = _settings()
    app.state.pool = pool
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _model_payload(name: str = "主模型", enabled: bool | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "provider": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "model": "qwen-plus",
        "secret": "sk-should-never-be-echoed",
    }
    if enabled is not None:
        payload["enabled"] = enabled
    payload.update(extra)
    return payload


async def test_create_list_update_delete(db_pool: AsyncConnectionPool) -> None:
    created = await repo.create_connection(db_pool, "model", _model_payload("甲模型"))
    assert created["enabled"] is True  # 保存即启用：未显式给 enabled 默认开启
    assert created["secrets_configured"] == {"secret": True}
    assert "sk-should-never-be-echoed" not in str(created)  # 凭据不回显

    items = await repo.list_connections(db_pool, "model")
    assert [item["id"] for item in items] == [created["id"]]

    updated = await repo.update_connection(db_pool, "model", created["id"], {"name": "乙模型"})
    assert updated["name"] == "乙模型"
    assert updated["secrets_configured"] == {"secret": True}  # 不提交凭据时保持原值

    await repo.delete_connection(db_pool, "model", created["id"])
    assert await repo.list_connections(db_pool, "model") == []


async def test_enabling_second_connection_disables_first(db_pool: AsyncConnectionPool) -> None:
    first = await repo.create_connection(db_pool, "model", _model_payload("第一套"))
    second = await repo.create_connection(db_pool, "model", _model_payload("第二套"))

    items = await repo.list_connections(db_pool, "model")
    states = {item["id"]: item["enabled"] for item in items}
    assert states[first["id"]] is False
    assert states[second["id"]] is True

    await repo.set_enabled(db_pool, "model", first["id"], True)
    items = await repo.list_connections(db_pool, "model")
    states = {item["id"]: item["enabled"] for item in items}
    assert states[first["id"]] is True
    assert states[second["id"]] is False


async def test_mcp_targets_are_independent(db_pool: AsyncConnectionPool) -> None:
    bocha = await repo.create_connection(db_pool, "mcp", {
        "name": "博查", "target": "bocha", "url": "https://mcp.bocha.example/sse",
        "tool_name": "web_search", "secret": "bocha-key",
    })
    qichacha = await repo.create_connection(db_pool, "mcp", {
        "name": "企查查", "target": "qichacha", "url": "https://mcp.qcc.example/sse",
        "tool_name": "qcc_search", "secret": "qcc-key",
    })
    items = await repo.list_connections(db_pool, "mcp")
    assert all(item["enabled"] for item in items if item["id"] in {bocha["id"], qichacha["id"]})


async def test_external_categories_support_ocr_asr_oss(db_pool: AsyncConnectionPool) -> None:
    """外部接口按分类维护：ocr/asr/oss 与 crm 互不影响，各自一条启用。"""
    ocr = await repo.create_connection(db_pool, "external", {
        "name": "阿里云 OCR", "target": "ocr",
        "endpoint": "ocr-api.cn-hangzhou.aliyuncs.com", "type": "Advanced",
        "output_coordinate": "points", "timeout_seconds": 30,
        "secrets": {"access_key_id": "ak-id", "access_key_secret": "ak-secret"},
    })
    asr = await repo.create_connection(db_pool, "external", {
        "name": "百炼 ASR", "target": "asr", "model": "paraformer-v2",
        "language_hints": "zh,en", "diarization_enabled": True,
        "secrets": {"api_key": "asr-key"},
    })
    assert ocr["secrets_configured"] == {"access_key_id": True, "access_key_secret": True}
    assert asr["secrets_configured"] == {"api_key": True}
    assert "ak-secret" not in str(ocr) and "asr-key" not in str(asr)
    assert asr["language_hints"] == ["zh", "en"]  # 字符串输入拆成列表

    records = await repo.list_records(db_pool)
    effective = merge_records(_settings(), records)
    assert effective.ocr.enabled is True
    assert effective.ocr.access_key_id == "ak-id"
    assert effective.ocr.access_key_secret == "ak-secret"
    assert effective.asr.enabled is True
    assert effective.asr.api_key == "asr-key"
    assert effective.asr.model == "paraformer-v2"
    assert effective.asr.language_hints == ["zh", "en"]


async def test_external_category_mutex_and_disable(db_pool: AsyncConnectionPool) -> None:
    """同分类新连接顶掉旧的；全部停用时该分类配置段回到禁用。"""
    first = await repo.create_connection(db_pool, "external", {
        "name": "OSS 一号", "target": "oss", "endpoint": "oss-a.example.com", "bucket": "b1",
        "secrets": {"access_key_id": "id1", "access_key_secret": "s1"},
    })
    second = await repo.create_connection(db_pool, "external", {
        "name": "OSS 二号", "target": "oss", "endpoint": "oss-b.example.com", "bucket": "b2",
        "secrets": {"access_key_id": "id2", "access_key_secret": "s2"},
    })
    items = await repo.list_connections(db_pool, "external")
    states = {item["id"]: item["enabled"] for item in items if item["target"] == "oss"}
    assert states[first["id"]] is False
    assert states[second["id"]] is True

    records = await repo.list_records(db_pool)
    effective = merge_records(_settings(), records)
    assert effective.oss.enabled is True
    assert effective.oss.bucket == "b2"

    await repo.set_enabled(db_pool, "external", second["id"], False)
    effective = merge_records(_settings(), await repo.list_records(db_pool))
    assert effective.oss.enabled is False


async def test_unknown_secret_field_rejected(db_pool: AsyncConnectionPool) -> None:
    from app.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        await repo.create_connection(db_pool, "external", {
            "name": "坏凭据", "target": "asr", "model": "paraformer-v2",
            "secrets": {"api_key": "k", "access_key_id": "not-allowed"},
        })


async def test_enabled_connection_becomes_effective_settings(db_pool: AsyncConnectionPool) -> None:
    await repo.create_connection(db_pool, "model", _model_payload("生效模型"))
    records = await repo.list_records(db_pool)
    effective = merge_records(_settings(), records)

    assert effective.model.provider == "openai_compatible"
    assert effective.model.base_url == "https://api.example.com/v1"
    assert effective.model.name == "qwen-plus"
    assert effective.model.api_key == "sk-should-never-be-echoed"


async def test_overview_reports_managed_and_credentials(db_pool: AsyncConnectionPool) -> None:
    await repo.create_connection(db_pool, "model", _model_payload("概览模型"))
    overview = await build_overview(db_pool, _settings())

    assert overview["managed"]["model"]["name"] == "概览模型"
    assert overview["credentials"]["model.api_key"] is True
    assert overview["counts"]["model"]["enabled"] == 1
    assert overview["environment"] == "dev"


async def test_connection_api_requires_permission_and_validates(db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, PASSWORD)
    async with await _client(db_pool) as client:
        login = await client.post("/api/v1/auth/login",
                                  json={"username": "admin", "password": PASSWORD})
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = await client.post("/api/v1/settings/connections/model",
                                    json=_model_payload("接口模型"), headers=headers)
        assert created.status_code == 200
        assert "sk-should-never-be-echoed" not in created.text

        invalid = await client.post("/api/v1/settings/connections/model",
                                    json={"name": "坏字段", "unknown_field": 1}, headers=headers)
        assert invalid.status_code == 422

        listed = await client.get("/api/v1/settings/connections", headers=headers)
        assert listed.status_code == 200
        kinds = {item["kind"] for item in listed.json()["kinds"]}
        assert kinds == {"model", "external", "mcp"}

        connection_id = created.json()["id"]
        removed = await client.delete(f"/api/v1/settings/connections/model/{connection_id}",
                                      headers=headers)
        assert removed.status_code == 200


async def test_disabled_only_target_keeps_base_config(db_pool: AsyncConnectionPool) -> None:
    """全部停用时不改写基础配置段，避免静默降级。"""
    await repo.create_connection(db_pool, "model", _model_payload("停用模型", enabled=False))
    records = await repo.list_records(db_pool)
    effective = merge_records(_settings(), records)
    assert effective.model.provider == "stub"
