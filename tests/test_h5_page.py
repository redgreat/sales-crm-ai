"""H5 测试联调页（/h5）：静态页挂载、开关门禁、会话与 CRM 代理的前置校验。

三门硬约束：
1. 只有 dev/test 且 `api.playground.enabled` 时才可用，prod 一律 404（联调面不进生产）；
2. 页面必须注入 `window.__H5_BASE='/h5'`，否则 `/crm/**` 会打到站点根路径而 404；
3. 没有有效会话时 `/h5/crm/**` 必须 401——浏览器不持任何密钥，token 只存服务端内存。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from app.admin import repo
from app.admin.paths import playground_enabled
from app.devtools.h5 import config as h5_config
from app.devtools.h5 import router as h5_router
from app.devtools.h5 import session as h5_session
from app.devtools.h5 import static as h5_static
from app.config import PROJECT_ROOT, Settings
from app.errors import ApiError

pytestmark = pytest.mark.realpg

PROD_SETTINGS = Settings(environment="prod", model={"provider": "openai_compatible",
                                                    "base_url": "https://api.example.com/v1",
                                                    "name": "qwen-plus",
                                                    "api_key": "unit-model-key"},
                         auth={"service_secret": "prod-unit-secret"})
DEV_SETTINGS = Settings(environment="dev", api={"playground": {"enabled": True}},
                        model={"provider": "stub"})


def _app(settings: Settings, pool: AsyncConnectionPool | None = None) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _handler(request, exc):  # noqa: ANN001, ARG001
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    app.include_router(h5_router)
    app.state.settings = settings
    if pool is not None:
        app.state.pool = pool
    return app


def _client(settings: Settings) -> TestClient:
    return TestClient(_app(settings))


# --------------------------------------------------------------------------- 纯逻辑


def test_base_script_is_injected_once() -> None:
    page = h5_static.index_file()
    assert page is not None, "tests/h5/index.html 应在仓库内"
    body = h5_static.render_index(page).decode("utf-8")
    assert body.count(h5_static.BASE_SCRIPT) == 1
    assert "window.__H5_BASE='/h5'" in body


def test_page_calls_are_base_aware() -> None:
    """页面自身的 /crm、/__login 等调用必须带 H5_BASE 前缀。"""
    page = (PROJECT_ROOT / "tests" / "h5" / "index.html").read_text(encoding="utf-8")
    assert "base: `${H5_BASE}/crm`" in page
    assert "fetch(`${H5_BASE}/__login`" in page
    assert "fetch(`${H5_BASE}/__session`" in page
    assert "fetch('/__login'" not in page


def test_as_text_joins_lists() -> None:
    assert h5_config.as_text(["a", " b ", ""]) == "a b"
    assert h5_config.as_text("x y") == "x y"
    assert h5_config.as_text(None) == ""


def test_playground_switch_rejects_prod() -> None:
    assert playground_enabled(DEV_SETTINGS) is True
    assert playground_enabled(PROD_SETTINGS) is False


# --------------------------------------------------------------------------- 路由门禁


def test_h5_page_404_in_production() -> None:
    response = _client(PROD_SETTINGS).get("/h5/")
    assert response.status_code == 404
    assert "未启用" in response.text


def test_h5_page_serves_index_with_base() -> None:
    response = _client(DEV_SETTINGS).get("/h5/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "window.__H5_BASE='/h5'" in response.text


def test_crm_proxy_requires_session() -> None:
    response = _client(DEV_SETTINGS).get("/h5/crm/ai/capabilities")
    assert response.status_code == 401
    assert response.json()["ok"] is False


def test_session_endpoint_reports_disconnected() -> None:
    response = _client(DEV_SETTINGS).get("/h5/__session")
    assert response.status_code == 200
    assert response.json()["connected"] is False


def test_manual_token_without_exp_is_rejected() -> None:
    """无法解析 exp 的 token 一律视为无效：不让无过期时间的 token 长期驻留内存。"""
    response = _client(DEV_SETTINGS).post("/h5/__login", json={"token": "not-a-jwt"})
    assert response.status_code == 400
    assert response.json()["ok"] is False


# --------------------------------------------------------------------------- 库内 CRM 连接


async def test_crm_base_comes_from_connection_record(db_pool: AsyncConnectionPool) -> None:
    await repo.create_connection(
        db_pool,
        "external",
        {"name": "CRM", "target": "crm",
         "base_url": "https://crm.example.com/api/v1/salescrm",
         "client_id": "crm-client", "scopes": "salescrm"},
        DEV_SETTINGS,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=db_pool,
                                                                       settings=DEV_SETTINGS)))
    base = await h5_config.crm_base(request)
    assert base == "https://crm.example.com/api/v1/salescrm"

    oauth = await h5_config.oauth_config(request)
    assert oauth["client_id"] == "crm-client"
    assert oauth["scopes"] == "salescrm"
    assert oauth["token_endpoint"] == h5_config.DEFAULT_TOKEN_ENDPOINT
    assert oauth["client_secret"] == ""  # 未配置：客户端密钥绝不回显


async def test_client_secret_is_read_but_never_exposed(db_pool: AsyncConnectionPool) -> None:
    await repo.create_connection(
        db_pool,
        "external",
        {"name": "CRM", "target": "crm", "base_url": "https://crm.example.com/api",
         "secrets": {"client_secret": "oidc-secret-value", "secret": "hmac-secret"}},
        DEV_SETTINGS,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=db_pool,
                                                                       settings=DEV_SETTINGS)))
    oauth = await h5_config.oauth_config(request)
    assert oauth["client_secret"] == "oidc-secret-value"

    # 对外接口只回「是否已配置」，明文绝不出现在任何对外载荷里
    listed = await repo.list_connections(db_pool, "external")
    public = next(item for item in listed if item.get("target") == "crm")
    assert public["secrets_configured"]["client_secret"] is True
    assert "oidc-secret-value" not in str(public)
    assert "client_secret" not in public
