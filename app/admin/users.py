"""管理端账号的口令与角色规则（纯函数，不含存储）。

账号与权限点存 PG（见 app.admin.repo）；口令用 PBKDF2-HMAC-SHA256（20 万次迭代 +
随机盐），库里只有 `pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>`，不存明文、不进日志。
每个用户带 `token_version`：改密 / 重置 / 停用后自增，使已签发 Token 立即失效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

ROLES = ("admin", "operator", "viewer")
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "admin": ("settings:read", "connections:manage", "users:manage"),
    "operator": ("settings:read", "connections:manage"),
    "viewer": ("settings:read",),
}
_ITERATIONS = 200_000
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8


def role_permissions(role: str) -> list[str]:
    return list(PERMISSIONS.get(role, ()))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        _ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return hmac.compare_digest(actual, expected)


def check_username(username: str) -> str:
    username = username.strip()
    if not _USERNAME.match(username):
        raise_username_error()
    return username


def raise_username_error() -> None:
    from app.errors import ValidationFailed

    raise ValidationFailed("用户名需为 3-32 位字母、数字、下划线、点或短横线")


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        from app.errors import ValidationFailed

        raise ValidationFailed(f"口令至少 {MIN_PASSWORD_LENGTH} 位")


def check_role(role: str) -> str:
    if role not in ROLES:
        from app.errors import ValidationFailed

        raise ValidationFailed(f"角色必须是 {', '.join(ROLES)} 之一")
    return role


def public_user(user: dict[str, object]) -> dict[str, object]:
    """对外可见字段：绝不包含 password_hash。"""
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "display_name": user.get("display_name"),
        "role": user.get("role"),
        "disabled": bool(user.get("disabled")),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "last_login_at": user.get("last_login_at"),
        "permissions": role_permissions(str(user.get("role", "viewer"))),
    }


def losing_last_admin(current: dict[str, object], remaining_admins: int,
                      role: str | None = None, disabled: bool | None = None) -> bool:
    """是否会把最后一个可用管理员降权或停用——那会让后台彻底锁死。"""
    is_admin = str(current.get("role")) == "admin" and not current.get("disabled")
    if not is_admin:
        return False
    losing = (role is not None and role != "admin") or disabled is True
    return losing and remaining_admins == 0
