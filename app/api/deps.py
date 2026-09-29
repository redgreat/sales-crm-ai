"""公共依赖：服务间认证（签名 + 时效 + 防重放）与操作者上下文。

每轮请求重新鉴权；operator 不落浏览器、不可由其任意指定身份字段以外的范围。
"""
from __future__ import annotations

from fastapi import Request

from app.auth import OperatorContext, verify_signed_request
from app.errors import Unauthenticated
from app.persistence.nonces import register_nonce


async def require_operator(request: Request) -> OperatorContext:
    settings = request.app.state.settings
    body = await request.body()  # Starlette 缓存 body，后续 Body 解析复用

    if settings.environment == "test" and not request.headers.get("x-sai-auth"):
        # 仅测试环境允许无签名调用（契约测试用）；生产/开发必须签名
        user_id = request.headers.get("x-sai-user-id", "")
        if not user_id:
            raise Unauthenticated("缺少操作者身份")
        return OperatorContext(user_id=user_id, user_name=request.headers.get("x-sai-user-name", ""))

    if not settings.auth.service_secret:
        raise Unauthenticated("服务端未配置认证密钥，拒绝 API 调用")

    verified = verify_signed_request(
        method=request.method,
        path=request.url.path,
        body=body,
        headers=request.headers,
        secret_provider=lambda key_id: (
            settings.auth.service_secret if key_id == settings.auth.service_key_id else None
        ),
        timestamp_window_seconds=settings.auth.timestamp_window_seconds,
    )
    accepted = await register_nonce(
        request.app.state.pool,
        nonce=verified.nonce,
        key_id=verified.key_id,
        ttl_seconds=settings.auth.nonce_ttl_seconds,
    )
    if not accepted:
        raise Unauthenticated("重放请求被拒绝（nonce 已使用）")
    if verified.operator is None:
        raise Unauthenticated("缺少操作者身份")
    return verified.operator
