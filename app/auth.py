"""服务间认证：HMAC-SHA256 请求签名 + 时间窗 + 防重放（nonce 持久化）。

CRM -> AI 与 AI -> CRM 使用同一方案。身份字段（操作者）参与签名，
浏览器不能任意指定；AI 不持有模型密钥下发给任何调用方。

签名串（\\n 连接）:
    key_id
    method（大写）
    path（含 query 之前的路径）
    timestamp（unix 秒）
    nonce
    operator_user_id（可空字符串）
    body_sha256_hex（空 body 为空字符串的 sha256）
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote, unquote

from app.errors import Unauthenticated

SIGN_ALGORITHM = "SAI-HMAC-SHA256"
HEADER_AUTH = "X-SAI-Auth"
HEADER_KEY_ID = "X-SAI-Key-Id"
HEADER_TIMESTAMP = "X-SAI-Timestamp"
HEADER_NONCE = "X-SAI-Nonce"
HEADER_SIGNATURE = "X-SAI-Signature"
HEADER_USER_ID = "X-SAI-User-Id"
HEADER_USER_NAME = "X-SAI-User-Name"


@dataclass(frozen=True)
class OperatorContext:
    """发起操作的用户上下文；每轮请求重新鉴权后产生。"""

    user_id: str
    user_name: str = ""


def canonical_string(
    *,
    key_id: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    user_id: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return "\n".join([key_id, method.upper(), path, timestamp, nonce, user_id, body_hash])


def compute_signature(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_request(
    *,
    secret: str,
    key_id: str,
    method: str,
    path: str,
    body: bytes = b"",
    operator: OperatorContext | None = None,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """出站/测试用：生成带签名的请求头集合。"""
    ts = str(timestamp if timestamp is not None else int(time.time()))
    n = nonce or uuid.uuid4().hex
    user_id = operator.user_id if operator else ""
    canonical = canonical_string(
        key_id=key_id,
        method=method,
        path=path,
        timestamp=ts,
        nonce=n,
        user_id=user_id,
        body=body,
    )
    headers = {
        HEADER_AUTH: SIGN_ALGORITHM,
        HEADER_KEY_ID: key_id,
        HEADER_TIMESTAMP: ts,
        HEADER_NONCE: n,
        HEADER_SIGNATURE: compute_signature(secret, canonical),
    }
    if operator:
        headers[HEADER_USER_ID] = operator.user_id
        if operator.user_name:
            # HTTP 头仅允许 ASCII；非 ASCII 用户名用百分号编码传输
            headers[HEADER_USER_NAME] = quote(operator.user_name, safe="")
    return headers


@dataclass(frozen=True)
class VerifiedRequest:
    operator: OperatorContext | None
    key_id: str
    nonce: str


def verify_signed_request(
    *,
    method: str,
    path: str,
    body: bytes,
    headers,
    secret_provider,
    timestamp_window_seconds: int,
    now: float | None = None,
) -> VerifiedRequest:
    """入站校验。secret_provider: callable(key_id) -> str | None。

    只做签名/时效校验；nonce 防重放由调用方在持久层完成（见 persistence.nonces）。
    """
    current = now if now is not None else time.time()
    if headers.get(HEADER_AUTH) != SIGN_ALGORITHM:
        raise Unauthenticated("缺少或错误的认证方案")
    key_id = headers.get(HEADER_KEY_ID, "")
    ts_raw = headers.get(HEADER_TIMESTAMP, "")
    nonce = headers.get(HEADER_NONCE, "")
    signature = headers.get(HEADER_SIGNATURE, "")
    if not (key_id and ts_raw and nonce and signature):
        raise Unauthenticated("认证头不完整")
    secret = secret_provider(key_id)
    if secret is None:
        raise Unauthenticated("未知 key_id")
    try:
        ts = float(ts_raw)
    except ValueError as exc:
        raise Unauthenticated("时间戳格式错误") from exc
    if abs(current - ts) > timestamp_window_seconds:
        raise Unauthenticated("请求时间戳超出允许窗口")
    user_id = headers.get(HEADER_USER_ID, "")
    canonical = canonical_string(
        key_id=key_id,
        method=method,
        path=path,
        timestamp=ts_raw,
        nonce=nonce,
        user_id=user_id,
        body=body,
    )
    expected = compute_signature(secret, canonical)
    if not hmac.compare_digest(expected, signature):
        raise Unauthenticated("签名校验失败")
    operator = (
        OperatorContext(user_id=user_id, user_name=unquote(headers.get(HEADER_USER_NAME, "")))
        if user_id
        else None
    )
    return VerifiedRequest(operator=operator, key_id=key_id, nonce=nonce)
