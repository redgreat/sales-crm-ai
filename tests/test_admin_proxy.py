"""开发代理契约：管理端请求必须能穿过 /playground/api 原样到达业务路由。

历史上这里有两个坑：代理只放行 GET/POST/DELETE（保存配置的 PUT 直接 405），
以及重建请求头时把浏览器的 Authorization 丢掉（登录成功却仍判未登录）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import httpx
from psycopg_pool import AsyncConnectionPool

from app.admin import paths, repo
from app.api.routes import auth as auth_routes
from app.api.routes import playground as playground_routes
from app.config import Settings
from app.errors import ApiError

SECRET = "proxy-unit-secret"


def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    conf = tmp_path / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "config.yml").write_text(
        "environment: dev\napi:\n  playground:\n    enabled: true\nmodel:\n  provider: stub\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAI_CONFIG", str(conf / "config.yml"))
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)

    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _handler(request, exc):  # noqa: ARG001
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    seen: dict[str, str] = {}

    @app.put("/api/v1/echo")
    async def echo_put(request: Request) -> dict:
        seen["authorization"] = request.headers.get("authorization", "")
        return {"method": "PUT", "body": (await request.body()).decode()}

    @app.get("/api/v1/whoami")
    async def whoami(request: Request) -> dict:
        return {"authorization": request.headers.get("authorization", "")}

    app.include_router(playground_routes.router)
    app.include_router(auth_routes.router)
    app.state.settings = Settings(
        environment="dev",
        api={"playground": {"enabled": True}},
        auth={"service_secret": SECRET},
    )
    app.state.seen = seen
    return TestClient(app)


def test_put_is_forwarded_with_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = build(tmp_path, monkeypatch)
    response = client.put("/playground/api/v1/echo", json={"hello": "world"})
    assert response.status_code == 200, response.text
    assert response.json() == {"method": "PUT", "body": '{"hello":"world"}'}


def test_authorization_header_survives_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = build(tmp_path, monkeypatch)
    response = client.get("/playground/api/v1/whoami", headers={"Authorization": "Bearer abc.def"})
    assert response.status_code == 200
    assert response.json()["authorization"] == "Bearer abc.def"


@pytest.mark.realpg
async def test_full_login_flow_through_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, None, "proxy-pass-12345")
    app = build(tmp_path, monkeypatch).app
    app.state.pool = db_pool
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        login = await client.post(
            "/playground/api/v1/auth/login",
            json={"username": "admin", "password": "proxy-pass-12345"},
        )
        assert login.status_code == 200, login.text
        token = login.json()["token"]

        me = await client.get("/playground/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        assert me.json()["username"] == "admin"


def test_proxy_is_disabled_in_production(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = build(tmp_path, monkeypatch)
    prod = Settings(environment="prod", api={"playground": {"enabled": False}},
                    model={"provider": "openai_compatible", "base_url": "https://x/v1",
                           "api_key": "k", "name": "m"}, auth={"service_secret": SECRET})
    client.app.state.settings = prod
    assert client.get("/playground/api/v1/whoami").status_code == 404
