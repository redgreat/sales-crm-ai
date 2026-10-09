"""配置后台的 PG 仓储：连接列表与账号体系。

后台可配置的东西全部落库（不再写 conf 下的 YAML）：
- `admin_connections`：模型服务 / 外部接口 / MCP 三类连接及随连接的凭据
- `admin_users`：账号、角色、PBKDF2 口令、token_version

凭据语义：按分类定义若干凭据字段（TARGET_SECRETS），写入 credential JSONB，
只写不回显，响应里只有 `secrets_configured`（每个字段是否已配置）。
"""
from __future__ import annotations

import secrets as _random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.admin import users as user_rules
from app.admin.schema import (KINDS, TARGET_FIELDS, TARGET_SECRETS, check_secrets,
                              coerce_fields, resolve_target, split_secrets)
from app.errors import Conflict, NotFound, ValidationFailed

_CONNECTION_COLUMNS = """
    id, kind, target, name, enabled, credential, config, created_at, updated_at
"""


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    config = row.get("config") or {}
    credential = row.get("credential") or {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "target": row["target"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "secrets_configured": {
            key: bool(credential.get(key)) for key in TARGET_SECRETS.get(row["target"], ())
        },
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        **{key: value for key, value in config.items() if value is not None},
    }


def _existing(row: dict[str, Any]) -> dict[str, Any]:
    """把库行还原成校验函数期望的「扁平记录」。"""
    return {
        "id": row["id"],
        "target": row["target"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        **(row.get("config") or {}),
    }


async def _rows(pool: psycopg.AsyncConnectionPool, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())


async def _one(pool: psycopg.AsyncConnectionPool, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    rows = await _rows(pool, sql, params)
    if not rows:
        raise NotFound("记录不存在")
    return rows[0]


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------


async def list_connections(pool: psycopg.AsyncConnectionPool, kind: str) -> list[dict[str, Any]]:
    rows = await _rows(
        pool,
        f"SELECT {_CONNECTION_COLUMNS} FROM admin_connections WHERE kind = %s ORDER BY created_at, name",
        (kind,),
    )
    return [_public(row) for row in rows]


async def list_records(pool: psycopg.AsyncConnectionPool) -> list[dict[str, Any]]:
    """生效配置用：全部记录（含凭据明文，仅服务端内部使用，绝不外传）。"""
    return await _rows(
        pool,
        "SELECT id, kind, target, name, enabled, credential, config "
        "FROM admin_connections ORDER BY kind, target, created_at",
    )


async def counts(pool: psycopg.AsyncConnectionPool) -> dict[str, dict[str, int]]:
    rows = await _rows(
        pool,
        "SELECT kind, count(*) AS total, count(*) FILTER (WHERE enabled) AS enabled "
        "FROM admin_connections GROUP BY kind",
    )
    summary = {
        kind: {"total": 0, "enabled": 0} for kind in KINDS
    }
    for row in rows:
        summary[row["kind"]] = {"total": int(row["total"]), "enabled": int(row["enabled"])}
    return summary


async def get_connection(pool: psycopg.AsyncConnectionPool, kind: str, connection_id: str) -> dict[str, Any]:
    row = await _one(
        pool,
        f"SELECT {_CONNECTION_COLUMNS} FROM admin_connections WHERE kind = %s AND id = %s",
        (kind, connection_id),
    )
    return _public(row)


async def create_connection(pool: psycopg.AsyncConnectionPool, kind: str,
                            payload: dict[str, Any],
                            base_settings: Any | None = None) -> dict[str, Any]:
    body, secrets = split_secrets(payload)
    target = resolve_target(kind, body)
    checked = check_secrets(target, secrets)
    record = coerce_fields(body, kind, target, None)
    connection_id = _random.token_hex(6)
    enabled = bool(record.get("enabled"))
    config = {key: value for key, value in record.items()
              if key in TARGET_FIELDS[target] and value is not None}
    if base_settings is not None:
        _assert_valid(base_settings, [(kind, target, record, checked)])
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if enabled:
                    await cur.execute(
                        "UPDATE admin_connections SET enabled = false, updated_at = now() "
                        "WHERE kind = %s AND target = %s",
                        (kind, target),
                    )
                await cur.execute(
                    "INSERT INTO admin_connections "
                    "(id, kind, target, name, enabled, credential, config) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (connection_id, kind, target, record["name"], enabled,
                     Jsonb(checked or {}), Jsonb(config)),
                )
    return await get_connection(pool, kind, connection_id)


async def update_connection(pool: psycopg.AsyncConnectionPool, kind: str, connection_id: str,
                            payload: dict[str, Any],
                            base_settings: Any | None = None) -> dict[str, Any]:
    body, secrets = split_secrets(payload)
    row = await _one(
        pool,
        "SELECT id, kind, target, name, enabled, credential, config "
        "FROM admin_connections WHERE kind = %s AND id = %s",
        (kind, connection_id),
    )
    current = _existing(row)
    target = resolve_target(kind, body, current)
    checked = check_secrets(target, secrets)
    merged = coerce_fields(body, kind, target, current)
    enabled = bool(merged.get("enabled", row["enabled"]))
    previous_config = row.get("config") or {}
    if target != row["target"]:
        previous_config = {}  # 分类变了：旧分类的业务字段整体作废
    config = {
        **{key: value for key, value in previous_config.items()},
        **{key: value for key, value in merged.items() if key in TARGET_FIELDS[target] and value is not None},
    }
    stored_credential: dict[str, Any] = {
        key: value for key, value in (row.get("credential") or {}).items()
        if key in TARGET_SECRETS[target]
    }
    # 未提交的凭据字段保持原值，提交了的覆盖
    stored_credential.update(checked)
    if base_settings is not None:
        _assert_valid(base_settings, [(kind, target, merged, stored_credential)])
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if enabled:
                    await cur.execute(
                        "UPDATE admin_connections SET enabled = false, updated_at = now() "
                        "WHERE kind = %s AND target = %s AND id <> %s",
                        (kind, target, connection_id),
                    )
                await cur.execute(
                    "UPDATE admin_connections SET target = %s, name = %s, enabled = %s, "
                    "credential = %s, config = %s, updated_at = now() "
                    "WHERE id = %s",
                    (target, merged["name"], enabled, Jsonb(stored_credential),
                     Jsonb(config), connection_id),
                )
    return await get_connection(pool, kind, connection_id)


async def delete_connection(pool: psycopg.AsyncConnectionPool, kind: str, connection_id: str) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM admin_connections WHERE kind = %s AND id = %s",
                (kind, connection_id),
            )
            if cur.rowcount == 0:
                raise NotFound("连接不存在")


async def set_enabled(pool: psycopg.AsyncConnectionPool, kind: str, connection_id: str,
                      enabled: bool) -> dict[str, Any]:
    return await update_connection(pool, kind, connection_id, {"enabled": enabled})


def _assert_valid(base_settings: Any, pending: list[tuple[str, str, dict[str, Any], dict[str, str]]]) -> None:
    """写前预校验：把待落库的连接并入配置做一次 Settings 校验，非法直接拒绝。"""
    from app.admin.runtime import apply_records
    from app.config import Settings

    data = base_settings.model_dump()
    records = [
        {"kind": kind, "target": target, "name": record.get("name"), "enabled": True,
         "credential": credential, "config": record}
        for kind, target, record, credential in pending
    ]
    apply_records(data, records)
    try:
        Settings(**data)
    except Exception as exc:
        raise ValidationFailed(_validation_message(exc)) from None


def _validation_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        fields = [".".join(str(part) for part in item["loc"]) for item in errors()]
        return "配置字段无效：" + ", ".join(fields)
    return str(exc)


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------
_USER_COLUMNS = "id, username, display_name, role, password_hash, token_version, disabled, created_at, updated_at, last_login_at"


async def list_users(pool: psycopg.AsyncConnectionPool) -> list[dict[str, Any]]:
    rows = await _rows(pool, f"SELECT {_USER_COLUMNS} FROM admin_users ORDER BY created_at, username")
    return [user_rules.public_user(row) for row in rows]


async def _user(pool: psycopg.AsyncConnectionPool, user_id: str) -> dict[str, Any]:
    return await _one(pool, f"SELECT {_USER_COLUMNS} FROM admin_users WHERE id = %s", (user_id,))


async def find_user(pool: psycopg.AsyncConnectionPool, user_id: str) -> dict[str, Any] | None:
    rows = await _rows(pool, f"SELECT {_USER_COLUMNS} FROM admin_users WHERE id = %s", (user_id,))
    return rows[0] if rows else None


async def _remaining_admins(conn: psycopg.AsyncConnection | psycopg.AsyncCursor,
                            exclude_id: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM admin_users "
            "WHERE role = 'admin' AND disabled = false AND id <> %s",
            (exclude_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def is_empty(pool: psycopg.AsyncConnectionPool) -> bool:
    rows = await _rows(pool, "SELECT count(*) AS n FROM admin_users")
    return int(rows[0]["n"]) == 0


async def bootstrap_admin(pool: psycopg.AsyncConnectionPool, hint_path: Path | None = None,
                          password: str | None = None, username: str = "admin") -> dict[str, Any]:
    """首次启动引导：只在没有任何账号时建号；口令来自环境变量或随机生成。

    随机口令只写进 `conf/admin.bootstrap.txt`（600）并在日志提示一次，不经 API 返回。
    """
    generated = password is None
    if generated:
        password = _random.token_urlsafe(16)
    user_rules.check_password(password)
    user_rules.check_username(username)
    payload = {
        "id": _random.token_hex(6),
        "username": username,
        "display_name": "超级管理员",
        "role": "admin",
        "password_hash": user_rules.hash_password(password),
        "token_version": 1,
        "disabled": False,
    }
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO admin_users (id, username, display_name, role, password_hash, token_version) "
                    "VALUES (%(id)s, %(username)s, %(display_name)s, %(role)s, %(password_hash)s, %(token_version)s) "
                    "ON CONFLICT (username) DO NOTHING",
                    payload,
                )
                if cur.rowcount == 0:
                    raise Conflict(f"账号 {username} 已存在")
    if generated and hint_path is not None:
        _write_hint(hint_path, username, password)
    return {"username": username, "password": password if generated else "", "generated": generated}


def _write_hint(path: Path, username: str, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"首次启动自动创建的管理员账号：{username}\n初始口令：{password}\n"
        f"请登录后立即修改，并删除本文件。\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


async def ensure_bootstrap(pool: psycopg.AsyncConnectionPool,
                           hint_path: Path | None = None) -> dict[str, Any] | None:
    import os

    if not await is_empty(pool):
        return None
    return await bootstrap_admin(pool, hint_path, os.environ.get("SAI_ADMIN_PASSWORD") or None)


async def authenticate(pool: psycopg.AsyncConnectionPool, username: str,
                       password: str) -> dict[str, Any] | None:
    username = username.strip()
    rows = await _rows(
        pool,
        f"SELECT {_USER_COLUMNS} FROM admin_users WHERE username = %s",
        (username,),
    )
    if not rows:
        return None
    user = rows[0]
    if not user_rules.verify_password(password, str(user["password_hash"])):
        return None
    if user["disabled"]:
        return None
    return dict(user)


async def touch_login(pool: psycopg.AsyncConnectionPool, user_id: str) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE admin_users SET last_login_at = now() WHERE id = %s", (user_id,))


async def create_user(pool: psycopg.AsyncConnectionPool, username: str, password: str,
                      role: str, display_name: str = "") -> dict[str, Any]:
    username = user_rules.check_username(username)
    user_rules.check_password(password)
    user_rules.check_role(role)
    payload = {
        "id": _random.token_hex(6),
        "username": username,
        "display_name": display_name.strip() or username,
        "role": role,
        "password_hash": user_rules.hash_password(password),
        "token_version": 1,
    }
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO admin_users (id, username, display_name, role, password_hash, token_version) "
                        "VALUES (%(id)s, %(username)s, %(display_name)s, %(role)s, %(password_hash)s, %(token_version)s)",
                        payload,
                    )
                except psycopg.errors.UniqueViolation:
                    raise Conflict(f"账号 {username} 已存在") from None
    return user_rules.public_user(payload)


async def update_user(pool: psycopg.AsyncConnectionPool, user_id: str, actor_id: str = "",
                      **fields: Any) -> dict[str, Any]:
    if not fields:
        raise ValidationFailed("没有需要更新的字段")
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id, username, role, disabled FROM admin_users WHERE id = %s", (user_id,))
                row = await cur.fetchone()
                if row is None:
                    raise NotFound("用户不存在")
                role = str(fields["role"]) if fields.get("role") is not None else None
                if role is not None:
                    user_rules.check_role(role)
                disabled = bool(fields["disabled"]) if fields.get("disabled") is not None else None
                if user_rules.losing_last_admin(row, await _remaining_admins(conn, user_id),
                                                role, disabled):
                    raise Conflict("至少需要一个可用的管理员账号")
                assignments: list[str] = []
                params: list[Any] = []
                if fields.get("display_name") is not None:
                    assignments.append("display_name = %s")
                    params.append(str(fields["display_name"]).strip() or row["username"])
                if role is not None:
                    assignments.append("role = %s")
                    params.append(role)
                if disabled is not None:
                    assignments.append("disabled = %s")
                    params.append(disabled)
                    # 停用/启用都让已签发 Token 失效
                    assignments.append("token_version = token_version + 1")
                assignments.append("updated_at = now()")
                params.append(user_id)
                await cur.execute(
                    f"UPDATE admin_users SET {', '.join(assignments)} WHERE id = %s", tuple(params))
    return user_rules.public_user(await _user(pool, user_id))


async def reset_password(pool: psycopg.AsyncConnectionPool, user_id: str, password: str) -> None:
    user_rules.check_password(password)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE admin_users SET password_hash = %s, token_version = token_version + 1, "
                "updated_at = now() WHERE id = %s",
                (user_rules.hash_password(password), user_id),
            )
            if cur.rowcount == 0:
                raise NotFound("用户不存在")


async def change_password(pool: psycopg.AsyncConnectionPool, user_id: str,
                          old_password: str, new_password: str) -> None:
    user_rules.check_password(new_password)
    user = await _user(pool, user_id)
    if not user_rules.verify_password(old_password, str(user["password_hash"])):
        raise ValidationFailed("当前口令不正确")
    await reset_password(pool, user_id, new_password)


async def delete_user(pool: psycopg.AsyncConnectionPool, user_id: str, actor_id: str) -> None:
    if str(user_id) == str(actor_id):
        raise Conflict("不能删除当前登录的账号")
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id, role, disabled FROM admin_users WHERE id = %s", (user_id,))
                row = await cur.fetchone()
                if row is None:
                    raise NotFound("用户不存在")
                if user_rules.losing_last_admin(row, await _remaining_admins(conn, user_id),
                                                disabled=True):
                    raise Conflict("至少需要一个可用的管理员账号")
                await cur.execute("DELETE FROM admin_users WHERE id = %s", (user_id,))
