"""管理端会话：HMAC-SHA256 签名的无状态 Token（Bearer），默认 2 小时。

不引入服务端会话表：载荷自带过期时间与 `token_version`，
而 `token_version` 会在改密 / 重置 / 停用时自增，因此「改口令即踢下线」仍然成立。
签名密钥复用 `auth.service_secret`——和对外服务签名共用一处主密钥。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.admin.users import role_permissions

DEFAULT_TTL_SECONDS = 2 * 60 * 60


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_token(secret: str, user: dict[str, Any], *,
                ttl_seconds: int = DEFAULT_TTL_SECONDS, now: float | None = None) -> str:
    issued = now or time.time()
    payload = {
        "uid": str(user.get("id", "")),
        "uname": str(user.get("username", "")),
        "dname": str(user.get("display_name") or user.get("username") or ""),
        "role": str(user.get("role", "viewer")),
        "ver": int(user.get("token_version", 1)),
        "exp": int(issued + ttl_seconds),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_b64url(raw)}.{_b64url(signature)}"


def verify_token(secret: str, token: str, *, version_check: bool = True,
                 current_user: dict[str, Any] | None = None,
                 now: float | None = None) -> dict[str, Any] | None:
    """校验签名、过期与 token_version；任何一步不过都返回 None（不抛异常，由调用方转 401）。"""
    if not secret or not token or token.count(".") != 1:
        return None
    raw_part, signature_part = token.split(".")
    try:
        raw = _b64url_decode(raw_part)
        provided = _b64url_decode(signature_part)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) <= int(now or time.time()):
        return None
    if version_check and current_user is not None and int(payload.get("ver", -1)) != int(
        current_user.get("token_version", -1)
    ):
        return None
    if current_user is not None and current_user.get("disabled"):
        return None
    role = str(payload.get("role", "viewer"))
    return {
        "id": str(payload.get("uid", "")),
        "username": str(payload.get("uname", "")),
        "display_name": str(payload.get("dname") or payload.get("uname") or ""),
        "role": role,
        "ver": int(payload.get("ver", 0)),
        "permissions": role_permissions(role),
        "expires_at": int(payload.get("exp", 0)),
    }
