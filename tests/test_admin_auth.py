"""管理端鉴权契约（账号存 PG）：登录、权限点、用户管理、Token 完整性。

覆盖两类安全边界：
1. 未登录 / Token 过期 / 被篡改 → 401；角色越权 → 403（前端隐藏按钮不算数）；
2. 改密、重置、停用后旧 Token 立即失效（token_version）。
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from app.admin import repo
from app.admin.session import issue_token
from app.admin.users import hash_password, verify_password
from app.api.routes import auth as auth_routes
from app.api.routes import settings as settings_routes
from app.config import Settings
from app.errors import ApiError

pytestmark = pytest.mark.realpg

SECRET = "admin-unit-secret"
PASSWORD = "admin-pass-12345"


def _settings() -> Settings:
    return Settings(
        environment="dev",
        auth={"service_secret": SECRET},
        api={"playground": {"enabled": True}},
        model={"provider": "stub"},
    )


def _app(pool: AsyncConnectionPool) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _handler(request, exc):  # noqa: ANN001, ARG001
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.state.settings = _settings()
    app.state.pool = pool
    return app


async def _client(pool: AsyncConnectionPool) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(pool)),
                             base_url="http://test")


async def _login(client: httpx.AsyncClient, username: str = "admin",
                 password: str = PASSWORD) -> dict[str, Any]:
    response = await client.post("/api/v1/auth/login", json={"username": username,
                                                             "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_password_hash_is_salted_and_verifiable() -> None:
    first, second = hash_password("same-password"), hash_password("same-password")
    assert first != second  # 随机盐
    assert verify_password("same-password", first)
    assert verify_password("same-password", second)
    assert not verify_password("other-password", first)
    assert not verify_password("same-password", "not-a-hash")


async def test_login_returns_token_and_permissions(db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, PASSWORD)
    async with await _client(db_pool) as client:
        bad = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
        assert bad.status_code == 401
        body = await _login(client)
        assert body["token"].count(".") == 1
        assert "users:manage" in body["user"]["permissions"]
        assert PASSWORD not in str(body)


async def test_settings_require_session_then_permission(db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, PASSWORD)
    await repo.create_user(db_pool, "operator-one", "operator-pass-1", "operator", "运维")
    await repo.create_user(db_pool, "viewer-one", "viewer-pass-11", "viewer", "只读")
    async with await _client(db_pool) as client:
        assert (await client.get("/api/v1/settings")).status_code == 401

        admin = (await _login(client))["token"]
        assert (await client.get("/api/v1/settings", headers=_auth(admin))).status_code == 200

        operator = (await _login(client, "operator-one", "operator-pass-1"))["token"]
        assert (await client.get("/api/v1/auth/users", headers=_auth(operator))).status_code == 403
        assert (await client.get("/api/v1/settings", headers=_auth(operator))).status_code == 200

        viewer = (await _login(client, "viewer-one", "viewer-pass-11"))["token"]
        forbidden = await client.post("/api/v1/settings/connections/model",
                                      json={"name": "x"}, headers=_auth(viewer))
        assert forbidden.status_code == 403


async def test_tampered_and_expired_token_rejected(db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, PASSWORD)
    async with await _client(db_pool) as client:
        token = (await _login(client))["token"]
        raw, signature = token.split(".")
        tampered = f"{raw[:-2]}xy.{signature}"
        assert (await client.get("/api/v1/auth/me", headers=_auth(tampered))).status_code == 401

    account = await repo.authenticate(db_pool, "admin", PASSWORD)
    assert account is not None
    stale = issue_token(SECRET, account, ttl_seconds=-10, now=time.time() - 10)
    async with await _client(db_pool) as client:
        assert (await client.get("/api/v1/auth/me", headers=_auth(stale))).status_code == 401


async def test_reset_password_invalidates_old_token(db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, PASSWORD)
    async with await _client(db_pool) as client:
        token = (await _login(client))["token"]
        assert (await client.get("/api/v1/auth/me", headers=_auth(token))).status_code == 200

    users = await repo.list_users(db_pool)
    await repo.reset_password(db_pool, str(users[0]["id"]), "another-pass-99")

    async with await _client(db_pool) as client:
        assert (await client.get("/api/v1/auth/me", headers=_auth(token))).status_code == 401
        assert (await _login(client, "admin", "another-pass-99"))["user"]["username"] == "admin"


async def test_disable_user_blocks_session(db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, PASSWORD)
    await repo.create_user(db_pool, "operator-two", "operator-pass-2", "operator", "运维")
    users = await repo.list_users(db_pool)
    admin_id = str(users[0]["id"])
    target = next(item for item in users if item["username"] == "operator-two")
    await repo.update_user(db_pool, str(target["id"]), admin_id, disabled=True)

    async with await _client(db_pool) as client:
        blocked = await client.post("/api/v1/auth/login",
                                    json={"username": "operator-two", "password": "operator-pass-2"})
        assert blocked.status_code == 401


async def test_last_admin_cannot_be_downgraded_or_removed(db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, PASSWORD)
    users = await repo.list_users(db_pool)
    admin_id = str(users[0]["id"])
    async with await _client(db_pool) as client:
        token = (await _login(client))["token"]
        assert (await client.put(f"/api/v1/auth/users/{admin_id}", json={"role": "viewer"},
                                 headers=_auth(token))).status_code == 409
        assert (await client.put(f"/api/v1/auth/users/{admin_id}", json={"disabled": True},
                                 headers=_auth(token))).status_code == 409
        assert (await client.delete(f"/api/v1/auth/users/{admin_id}",
                                    headers=_auth(token))).status_code == 409


async def test_change_password_rejects_wrong_old(db_pool: AsyncConnectionPool) -> None:
    await repo.bootstrap_admin(db_pool, PASSWORD)
    async with await _client(db_pool) as client:
        token = (await _login(client))["token"]
        response = await client.post("/api/v1/auth/me/password", json={
            "old_password": "not-my-password", "new_password": "brand-new-pass-1",
        }, headers=_auth(token))
        assert response.status_code == 422
