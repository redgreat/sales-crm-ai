"""配置后台 API：概览与连接列表（全部落 PG）。

鉴权：全部要求管理端会话；写操作按权限点二次校验（不信前端按钮显隐）。
凭据：只接受写入，响应里永远只有「是否已配置」。
基础配置（config.yml 直改）已取消：后台只管库里的连接与账号。
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.admin import repo
from app.admin.effective import refresh_app_settings
from app.admin.overview import build_overview
from app.admin.paths import require_admin_enabled
from app.admin.schema import KIND_TARGETS, KINDS, check_kind
from app.api import deps
from app.config import Settings

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class ConnectionBody(BaseModel):
    # 严格模式：未知字段一律 422，避免后台被当成任意配置写入口
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    target: str | None = None
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    url: str | None = None
    tool_name: str | None = None
    query_argument: str | None = None
    key_id: str | None = None
    endpoint: str | None = None
    type: str | None = None
    output_coordinate: str | None = None
    language_hints: list[str] | str | None = None
    diarization_enabled: bool | None = None
    bucket: str | None = None
    signed_url_ttl_seconds: int | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None
    max_retries: int | None = None
    # 凭据：只接受写入，响应里永远只有 secrets_configured；secret 为单凭据分类的简写
    secrets: dict[str, str] | None = None
    secret: str | None = None
    enabled: bool | None = None


def _pool(request: Request) -> psycopg.AsyncConnectionPool:
    return request.app.state.pool


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _payload(body: ConnectionBody) -> dict[str, Any]:
    return {key: value for key, value in body.model_dump().items() if value is not None}


@router.get("")
async def get_overview(request: Request,
                       _session: dict[str, Any] = Depends(deps.require_session),
                       ) -> dict[str, Any]:
    settings = _settings(request)
    require_admin_enabled(settings)
    return await build_overview(_pool(request), settings)


@router.get("/connections")
async def list_all_connections(request: Request,
                               _session: dict[str, Any] = Depends(deps.require_session),
                               ) -> dict[str, Any]:
    require_admin_enabled(_settings(request))
    pool = _pool(request)
    return {
        "kinds": [
            {"kind": kind, "targets": list(KIND_TARGETS[kind]),
             "items": await repo.list_connections(pool, kind)}
            for kind in KINDS
        ]
    }


@router.get("/connections/{kind}")
async def get_connections(kind: str, request: Request,
                          _session: dict[str, Any] = Depends(deps.require_session),
                          ) -> list[dict[str, Any]]:
    require_admin_enabled(_settings(request))
    return await repo.list_connections(_pool(request), check_kind(kind))


@router.post("/connections/{kind}")
async def post_connection(kind: str, body: ConnectionBody, request: Request,
                          _session: dict[str, Any] = Depends(deps.require_permissions("connections:manage")),
                          ) -> dict[str, Any]:
    require_admin_enabled(_settings(request))
    created = await repo.create_connection(_pool(request), check_kind(kind), _payload(body),
                                           _settings(request))
    await refresh_app_settings(request)
    return created


@router.get("/connections/{kind}/{connection_id}")
async def get_one_connection(kind: str, connection_id: str, request: Request,
                             _session: dict[str, Any] = Depends(deps.require_session),
                             ) -> dict[str, Any]:
    require_admin_enabled(_settings(request))
    return await repo.get_connection(_pool(request), check_kind(kind), connection_id)


@router.put("/connections/{kind}/{connection_id}")
async def put_connection(kind: str, connection_id: str, body: ConnectionBody, request: Request,
                         _session: dict[str, Any] = Depends(deps.require_permissions("connections:manage")),
                         ) -> dict[str, Any]:
    require_admin_enabled(_settings(request))
    updated = await repo.update_connection(_pool(request), check_kind(kind), connection_id,
                                           _payload(body), _settings(request))
    await refresh_app_settings(request)
    return updated


@router.delete("/connections/{kind}/{connection_id}")
async def delete_one_connection(kind: str, connection_id: str, request: Request,
                                _session: dict[str, Any] = Depends(deps.require_permissions("connections:manage")),
                                ) -> dict[str, Any]:
    require_admin_enabled(_settings(request))
    await repo.delete_connection(_pool(request), check_kind(kind), connection_id)
    await refresh_app_settings(request)
    return {"deleted": connection_id}
