"""开发联调代理：playground 前端经此访问 API。

浏览器不持服务密钥；代理在服务端注入固定联调身份（config.api.playground.operator_*）
并完成签名后转发到本进程 ASGI（不产生真实网络请求）。真实业务契约保持签名不变，
CRM 集成层上线后此代理可整体下线。

仅 dev/test 且 api.playground.enabled 时生效；prod 开启会在配置校验时拒绝启动。
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, Response

from app.admin.paths import playground_enabled
from app.auth import OperatorContext, sign_request
from app.errors import NotFound, ValidationFailed

router = APIRouter(prefix="/playground", tags=["playground"])

FORWARD_BODY_LIMIT = 1 << 20  # 1MB


def _proxy_response(response: httpx.Response) -> Response:
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


async def _forward(request: Request, forward_path: str, *, signed: bool) -> Response:
    settings = request.app.state.settings
    body = await request.body()
    if len(body) > FORWARD_BODY_LIMIT:
        raise ValidationFailed("请求体超过 1MB 限制")

    headers: dict[str, str] = {
        "content-type": request.headers.get("content-type", "application/json")
    }
    # 管理端会话 Token 由浏览器持有，必须原样透传，否则登录后仍被判未登录
    authorization = request.headers.get("authorization")
    if authorization:
        headers["authorization"] = authorization
    if signed:
        operator = OperatorContext(
            user_id=settings.api.playground.operator_user_id,
            user_name=settings.api.playground.operator_user_name,
        )
        headers.update(
            sign_request(
                secret=settings.auth.service_secret,
                key_id=settings.auth.service_key_id,
                method=request.method,
                path=forward_path,
                body=body,
                operator=operator,
            )
        )
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://playground.internal") as client:
        response = await client.request(request.method, forward_path, content=body, headers=headers)
    return _proxy_response(response)


@router.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_api(path: str, request: Request) -> Response:
    settings = request.app.state.settings
    if not playground_enabled(settings):
        raise NotFound("playground 联调代理未启用")
    return await _forward(request, f"/api/{path}", signed=True)


@router.get("/health")
async def proxy_health(request: Request) -> Response:
    return await _forward(request, "/health", signed=False)


@router.get("/ready")
async def proxy_ready(request: Request) -> Response:
    return await _forward(request, "/ready", signed=False)
