"""H5 联调页路由：`/h5` 页面、`/h5/__login|__logout|__session`、`/h5/crm/**`。

- 页面脚本通过注入的 `window.__H5_BASE='/h5'` 把同源调用落到本前缀下；
- OAuth 密码授权在服务端完成，`client_secret` 不出服务端、不进浏览器、不写日志；
- `/h5/crm/**` 同源代理到 CRM `base_url` 并注入用户 JWT，页面因此不受 CRM CORS 白名单限制。

开关与 `/playground` 代理一致：仅 dev/test 且 `api.playground.enabled`，prod 一律 404。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from app.admin.paths import playground_enabled
from app.config import Settings
from app.devtools.h5 import config, session, static
from app.errors import NotFound

logger = logging.getLogger("sales-crm-ai")

router = APIRouter(prefix="/h5", tags=["devtools"])

FORWARD_TIMEOUT = 60.0
TOKEN_TIMEOUT = 20.0


def _json(status: int, payload: dict[str, Any]) -> Response:
    return Response(content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    status_code=status, media_type="application/json")


def _require_playground(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if not playground_enabled(settings):
        raise NotFound("H5 联调页未启用（仅 dev/test 且 api.playground.enabled 时可用）")
    return settings


def _static_response(request: Request) -> Response:
    _require_playground(request)
    target = static.index_file()
    if target is None:
        return _json(404, {"ok": False,
                           "error": "H5 联调页未打进镜像（缺少 h5-dist/index.html）"})
    return Response(content=static.render_index(target), media_type="text/html; charset=utf-8",
                    headers={"cache-control": "no-cache"})


async def _password_grant(oauth: dict[str, str], username: str, password: str) -> str:
    if not oauth["client_secret"]:
        raise RuntimeError(
            "尚未配置 OAuth 客户端密钥：请在配置后台「外部接口 → CRM 接口」填写 client_secret，"
            "或设置环境变量 SAI_H5_CLIENT_SECRET"
        )
    data = {
        "grant_type": "password",
        "username": username,
        "password": password,
        "client_id": oauth["client_id"],
        "client_secret": oauth["client_secret"],
        "scope": oauth["scopes"],
    }
    async with httpx.AsyncClient(timeout=TOKEN_TIMEOUT) as client:
        response = await client.post(oauth["token_endpoint"], data=data)
    if response.status_code != 200:
        raise RuntimeError(f"登录失败 {response.status_code}：{response.text[:200]}")
    return str(response.json()["access_token"])


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = None
    password: str | None = None
    token: str | None = None
    keep_signed_in: bool = True


@router.post("/__login")
async def login(body: LoginBody, request: Request) -> Response:
    _require_playground(request)

    manual = (body.token or "").strip()
    username = (body.username or "").strip()
    password = body.password or ""

    if manual:
        current = session.new_session(session.normalize(manual), username, "manual")
        if not session.valid(current):
            return _json(400, {"ok": False, "error": "token 无法解析或已过期"})
        return _with_session(request, current, {"ok": True, **session.view(current)})

    if username and password:
        oauth = await config.oauth_config(request)
        try:
            token = await _password_grant(oauth, username, password)
        except Exception as exc:
            logger.info("H5 联调页登录失败（%s）", type(exc).__name__)
            return _json(401, {"ok": False, "error": str(exc)})
        current = session.new_session(session.normalize(token), username, "login")
        return _with_session(request, current, {"ok": True, **session.view(current)})

    reused = session.session_of(request)
    if reused is not None and session.valid(reused):
        return _json(200, {"ok": True, **session.view(reused), "reused": True})
    return _json(401, {"ok": False, "error": "未登录或 token 已过期，请用工号密码登录"})


@router.post("/__logout")
async def logout(request: Request) -> Response:
    session.drop(request)
    response = _json(200, {"ok": True})
    response.delete_cookie(session.COOKIE_NAME, path=session.COOKIE_PATH)
    return response


@router.get("/__session")
async def session_view(request: Request) -> Response:
    _require_playground(request)
    current = session.session_of(request)
    if current is None or not session.valid(current):
        return _json(200, {"ok": True, "connected": False, "username": "",
                           "expires_at": 0, "remaining_seconds": 0, "source": "",
                           "token_preview": ""})
    return _json(200, {"ok": True, **session.view(current)})


# 必须注册在静态兜底路由之前：/h5/crm/** 优先
@router.api_route("/crm/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def crm_proxy(path: str, request: Request) -> Response:
    _require_playground(request)

    current = session.session_of(request)
    if current is None or not session.valid(current):
        return _json(401, {"ok": False, "error": "未登录或 token 已过期，请先登录"})

    base = await config.crm_base(request)
    if not base:
        return _json(502, {"ok": False,
                           "error": "未配置 CRM 地址：请在配置后台「外部接口 → CRM 接口」填写 base_url"})

    body = await request.body()
    url = f"{base}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = {
        "Authorization": str(current.get("token", "")),
        "Accept": request.headers.get("accept", "application/json"),
    }
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    try:
        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT) as client:
            upstream = await client.request(request.method, url, headers=headers,
                                            content=body or None, follow_redirects=False)
    except httpx.HTTPError as exc:
        return _json(502, {"ok": False, "error": f"CRM 不可达：{exc}"})
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"))


@router.get("/", include_in_schema=False)
@router.get("/{path:path}", include_in_schema=False)
async def h5_page(request: Request) -> Response:
    # 单文件页面：除上面的接口与 /crm 代理外，其余路径一律回落 index.html（SPA 行为）
    return _static_response(request)


def _with_session(request: Request, current: dict[str, Any],
                  payload: dict[str, Any]) -> Response:
    response = _json(200, payload)
    session.persist(response, request, current)
    return response
