"""H5 联调页的 CRM 连接与 OAuth 参数解析。

数据来源：配置后台「外部接口 → CRM 接口」的连接记录（含凭据明文，仅服务端内部使用）。
优先级：连接记录 > 环境变量 > 内置默认值（默认值均为非敏感公开信息）。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import psycopg
from fastapi import Request

from app.config import Settings

logger = logging.getLogger("sales-crm-ai")

# 非敏感默认值（与 CRM 侧公开的 OAuth client 一致，见 scripts/h5_server.py）
DEFAULT_TOKEN_ENDPOINT = "https://identity-fat.lunz.cn/connect/token"
DEFAULT_CLIENT_ID = "salescrm-client"
DEFAULT_SCOPES = "salescrm app-4754bb86-d82e-400c-9e68-936c4a195c8d uc-users-outside-api"


def as_text(value: Any) -> str:
    """配置值容忍字符串或列表（前端 tags 类字段），统一成空格分隔字符串。"""
    if isinstance(value, (list, tuple)):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


async def crm_record(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    """取库中启用的 CRM 连接的 config 与 credential（含明文，绝不外传）。"""
    pool: psycopg.AsyncConnectionPool | None = getattr(request.app.state, "pool", None)
    if pool is None:
        return {}, {}
    try:
        from app.admin import repo

        records = await repo.list_records(pool)
    except Exception:
        logger.warning("H5 联调页读取 CRM 连接失败，回落配置默认值", exc_info=True)
        return {}, {}
    active = next(
        (row for row in records
         if row.get("kind") == "external" and row.get("target") == "crm" and row.get("enabled")),
        None,
    )
    if active is None:
        return {}, {}
    return dict(active.get("config") or {}), dict(active.get("credential") or {})


async def crm_base(request: Request) -> str:
    config, _ = await crm_record(request)
    base = as_text(config.get("base_url"))
    if not base:
        base = os.environ.get("SAI_CRM_BASE_URL", "").strip()
    if not base:
        settings: Settings = request.app.state.settings
        base = str(settings.crm.base_url or "").strip()
    return base.rstrip("/")


async def oauth_config(request: Request) -> dict[str, str]:
    config, credential = await crm_record(request)
    return {
        "token_endpoint": as_text(config.get("token_endpoint"))
        or os.environ.get("SAI_H5_TOKEN_ENDPOINT", "").strip() or DEFAULT_TOKEN_ENDPOINT,
        "client_id": as_text(config.get("client_id"))
        or os.environ.get("SAI_H5_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID,
        "client_secret": as_text(credential.get("client_secret"))
        or os.environ.get("SAI_H5_CLIENT_SECRET", "").strip(),
        "scopes": as_text(config.get("scopes"))
        or os.environ.get("SAI_H5_SCOPES", "").strip() or DEFAULT_SCOPES,
    }
