"""管理端登录与用户角色：Bearer Token 会话 + PBKDF2 账号（存 PG）+ 角色权限点。

登录本身不需要服务间签名（浏览器到不了服务间签名链路），但仍然只在
「非生产 + playground 开启」时可用。用户管理接口一律要求 `users:manage`。
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.admin import repo
from app.admin.paths import admin_enabled, require_admin_enabled
from app.admin.session import DEFAULT_TTL_SECONDS, issue_token
from app.admin.users import MIN_PASSWORD_LENGTH, role_permissions
from app.api import deps
from app.config import Settings
from app.errors import Conflict, NotFound, Unauthenticated, ValidationFailed

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CreateUserBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    role: str
    display_name: str = ""


class UpdateUserBody(BaseModel):
    display_name: str | None = None
    role: str | None = None
    disabled: bool | None = None


class PasswordBody(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class ChangePasswordBody(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


def _pool(request: Request) -> psycopg.AsyncConnectionPool:
    return request.app.state.pool


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/bootstrap")
async def bootstrap_state(request: Request) -> dict[str, Any]:
    """是否还处于「没有任何账号」的首次启动状态。

    只回布尔值，不回任何凭据——初始口令写在 conf/admin.bootstrap.txt 里给本机运维看。
    """
    if not admin_enabled(_settings(request)):
        return {"enabled": False, "bootstrapped": True}
    return {"enabled": True, "bootstrapped": not await repo.is_empty(_pool(request))}


@router.post("/login")
async def login(body: LoginBody, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    require_admin_enabled(settings)
    account = await repo.authenticate(_pool(request), body.username, body.password)
    if account is None:
        raise Unauthenticated("用户名或口令不正确")
    await repo.touch_login(_pool(request), str(account["id"]))
    token = issue_token(settings.auth.service_secret, account)
    return {
        "token": token,
        "expires_in": DEFAULT_TTL_SECONDS,
        "user": {
            "id": account["id"],
            "username": account["username"],
            "display_name": account.get("display_name"),
            "role": account.get("role"),
            "permissions": role_permissions(str(account.get("role", "viewer"))),
        },
    }


@router.post("/logout")
async def logout(_session: dict[str, Any] = Depends(deps.require_session)) -> dict[str, bool]:
    """无状态会话：服务端无可清理，前端丢弃 Token 即可。"""
    return {"ok": True}


@router.get("/me")
async def me(session: dict[str, Any] = Depends(deps.require_session)) -> dict[str, Any]:
    return session


@router.get("/roles")
async def roles(_session: dict[str, Any] = Depends(deps.require_session)) -> dict[str, Any]:
    from app.admin.users import PERMISSIONS

    return {"roles": [{"role": role, "permissions": list(perms)} for role, perms in PERMISSIONS.items()]}


@router.get("/users")
async def get_users(request: Request,
                    _session: dict[str, Any] = Depends(deps.require_permissions("users:manage")),
                    ) -> list[dict[str, Any]]:
    return await repo.list_users(_pool(request))


@router.post("/users")
async def post_user(body: CreateUserBody, request: Request,
                    _session: dict[str, Any] = Depends(deps.require_permissions("users:manage")),
                    ) -> dict[str, Any]:
    from app.admin.users import check_role

    check_role(body.role)
    return await repo.create_user(_pool(request), body.username, body.password, body.role,
                                  body.display_name)


@router.put("/users/{user_id}")
async def put_user(user_id: str, body: UpdateUserBody, request: Request,
                   actor: dict[str, Any] = Depends(deps.require_permissions("users:manage")),
                   ) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise ValidationFailed("没有需要更新的字段")
    if fields.get("disabled") is True and str(user_id) == str(actor["id"]):
        raise Conflict("不能停用自己的账号")
    return await repo.update_user(_pool(request), user_id, str(actor["id"]), **fields)


@router.post("/users/{user_id}/password")
async def post_reset_password(user_id: str, body: PasswordBody, request: Request,
                              _session: dict[str, Any] = Depends(deps.require_permissions("users:manage")),
                              ) -> dict[str, bool]:
    await repo.reset_password(_pool(request), user_id, body.password)
    return {"ok": True}


@router.delete("/users/{user_id}")
async def remove_user(user_id: str, request: Request,
                      actor: dict[str, Any] = Depends(deps.require_permissions("users:manage")),
                      ) -> dict[str, Any]:
    await repo.delete_user(_pool(request), user_id, str(actor["id"]))
    return {"deleted": user_id}


@router.post("/me/password")
async def post_change_password(body: ChangePasswordBody, request: Request,
                               session: dict[str, Any] = Depends(deps.require_session),
                               ) -> dict[str, bool]:
    await repo.change_password(_pool(request), str(session["id"]), body.old_password,
                               body.new_password)
    return {"ok": True}
