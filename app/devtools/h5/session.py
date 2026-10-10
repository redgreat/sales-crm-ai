"""H5 联调页的进程内登录会话。

浏览器只持有一个 HttpOnly Cookie（会话标识），CRM 用户 JWT 只存服务端内存：
- 进程重启即全部失效（测试面不需要持久化）；
- 口令与 token 不进日志、不落盘、不出现在任何响应载荷里。
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from typing import Any

from fastapi import Request, Response

COOKIE_NAME = "ry_h5_session"
COOKIE_PATH = "/h5"

_SESSIONS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """解出 JWT payload（不校验签名，仅用于过期判断与展示归属账号）。"""
    raw = token.split(" ", 1)[1] if token.lower().startswith("bearer ") else token
    try:
        segment = raw.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        decoded = base64.urlsafe_b64decode(segment)
        payload = json.loads(decoded)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def normalize(token: str) -> str:
    token = (token or "").strip().strip('"')
    if not token:
        return ""
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def remaining(session: dict[str, Any]) -> int:
    return max(0, int(session.get("expires_at", 0)) - int(time.time()))


def valid(session: dict[str, Any] | None) -> bool:
    return bool(session) and bool(session.get("token")) and remaining(session) > 0


def new_session(token: str, username: str, source: str) -> dict[str, Any]:
    return {
        "token": token,
        "username": username,
        "expires_at": int(decode_jwt_payload(token).get("exp", 0)),
        "source": source,
    }


def view(session: dict[str, Any]) -> dict[str, Any]:
    """对外可见的会话状态：只给预览，绝不给完整 token。"""
    token = str(session.get("token", ""))
    return {
        "connected": valid(session),
        "username": str(session.get("username", "")),
        "expires_at": int(session.get("expires_at", 0)),
        "remaining_seconds": remaining(session),
        "source": str(session.get("source", "")),
        "token_preview": (token[:16] + "…") if token else "",
    }


def session_of(request: Request) -> dict[str, Any] | None:
    sid = request.cookies.get(COOKIE_NAME, "")
    if not sid:
        return None
    with _LOCK:
        session = _SESSIONS.get(sid)
        if session is None:
            return None
        if not valid(session):
            _SESSIONS.pop(sid, None)
            return None
        return dict(session)


def persist(response: Response, request: Request, session: dict[str, Any]) -> None:
    sid = secrets.token_urlsafe(24)
    with _LOCK:
        _SESSIONS[sid] = dict(session)
    response.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=min(remaining(session), 86400),
        httponly=True,
        samesite="lax",
        path=COOKIE_PATH,
        secure=request.url.scheme == "https",
    )


def drop(request: Request) -> None:
    sid = request.cookies.get(COOKIE_NAME, "")
    if not sid:
        return
    with _LOCK:
        _SESSIONS.pop(sid, None)
