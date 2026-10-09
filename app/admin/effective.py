"""生效配置的组装：config.yml 打底，库里的启用连接覆盖其接管段落。

后台可配置的内容全部存 PG；服务启动时（连接池就绪后）读一次，
后台写操作后再刷新一次——改动立即生效，不需要重启进程。
库不可用或数据非法时回落 config.yml，保证服务仍能起来（并打日志）。
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg
from fastapi import Request

from app.admin.runtime import apply_records
from app.config import Settings

logger = logging.getLogger("sales-crm-ai")


async def load_effective_settings(pool: psycopg.AsyncConnectionPool, base: Settings) -> Settings:
    from app.admin import repo

    try:
        records = await repo.list_records(pool)
    except Exception:
        logger.warning("后台配置读取失败，本次启动回落到 config.yml", exc_info=True)
        return base
    return merge_records(base, records)


def merge_records(base: Settings, records: list[dict[str, Any]]) -> Settings:
    if not records:
        return base
    data = base.model_dump()
    apply_records(data, records)
    try:
        return Settings(**data)
    except Exception:
        logger.error("库里的后台配置非法，回落到 config.yml", exc_info=True)
        return base


async def refresh_app_settings(request: Request) -> Settings:
    """后台写操作后调用：重算生效配置并写回 app.state（热生效）。"""
    pool: psycopg.AsyncConnectionPool | None = getattr(request.app.state, "pool", None)
    current: Settings = request.app.state.settings
    if pool is None:
        return current
    effective = await load_effective_settings(pool, current)
    request.app.state.settings = effective
    return effective


async def bootstrap_admin_account(pool: psycopg.AsyncConnectionPool, settings: Settings) -> None:
    """首次启动引导 admin 账号；失败不影响服务启动。"""
    from app.admin import repo
    from app.admin.paths import admin_enabled, bootstrap_hint_path

    if not admin_enabled(settings):
        return
    try:
        created = await repo.ensure_bootstrap(pool, bootstrap_hint_path())
    except Exception:
        logger.warning("管理端账号引导未完成（不影响服务启动）", exc_info=True)
        return
    if created:
        logger.warning(
            "管理端首次启动：已创建管理员账号 %s，初始口令见 %s（请登录后立即修改并删除该文件）",
            created["username"],
            bootstrap_hint_path(),
        )
