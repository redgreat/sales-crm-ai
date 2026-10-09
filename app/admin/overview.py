"""后台概览：环境、连接接管情况、密钥齐备情况。

所有后台可配置的密钥（模型 / CRM / OCR / ASR / OSS / MCP）都随连接存库；
概览只暴露「是否已配置」，明文永不回显。
"""
from __future__ import annotations

from typing import Any

import psycopg

from app.admin import repo
from app.admin.runtime import SECRET_TARGETS, secret_paths
from app.admin.schema import KIND_TARGETS, KINDS
from app.config import Settings

# (kind, target) → 概览里的配置段名
MANAGED_KEYS: dict[tuple[str, str], str] = {
    ("model", "model"): "model",
    ("external", "crm"): "crm",
    ("external", "ocr"): "ocr",
    ("external", "asr"): "asr",
    ("external", "oss"): "oss",
    ("mcp", "bocha"): "research.bocha",
    ("mcp", "qichacha"): "research.qichacha",
}

# 由后台接管的密钥路径（所有分类的全部凭据落点）
MANAGED_SECRET_PATHS = {
    dotted
    for kind in KINDS
    for target in KIND_TARGETS[kind]
    for dotted in secret_paths(kind, target).values()
}


async def build_overview(pool: psycopg.AsyncConnectionPool, settings: Settings) -> dict[str, Any]:
    records = await repo.list_records(pool)
    credentials = _credentials(settings, records)
    return {
        "environment": settings.environment,
        "credentials": credentials,
        "credential_specs": _specs(credentials),
        "managed": _managed(records),
        "counts": await repo.counts(pool),
        # 后台保存后即热生效，无需重启
        "restart_required": False,
    }


def _credentials(settings: Settings, records: list[dict[str, Any]]) -> dict[str, bool]:
    from app.admin.secrets import SECRET_PATHS

    status: dict[str, bool] = {}
    for dotted in SECRET_PATHS:
        if dotted in MANAGED_SECRET_PATHS:
            status[dotted] = _from_records(records, dotted)
        else:
            value: Any = settings
            for part in dotted.split("."):
                value = getattr(value, part)
            status[dotted] = bool(value)
    return status


def _from_records(records: list[dict[str, Any]], dotted: str) -> bool:
    for row in records:
        if not row.get("enabled"):
            continue
        kind, target = str(row.get("kind")), str(row.get("target"))
        paths = SECRET_TARGETS.get((kind, target), {})
        if dotted not in paths.values():
            continue
        credential = row.get("credential") or {}
        if any(credential.get(key) for key, path in paths.items() if path == dotted):
            return True
    return False


def _specs(status: dict[str, bool]) -> list[dict[str, Any]]:
    from app.admin.secrets import credential_specs

    return [{**spec, "configured": bool(status.get(spec["path"]))} for spec in credential_specs()]


def _managed(records: list[dict[str, Any]]) -> dict[str, Any]:
    managed: dict[str, Any] = {key: None for key in MANAGED_KEYS.values()}
    for kind in KINDS:
        for target in KIND_TARGETS[kind]:
            scoped = [row for row in records
                      if row.get("kind") == kind and row.get("target") == target]
            active = next((row for row in scoped if row.get("enabled")), None)
            managed[MANAGED_KEYS[(kind, target)]] = (
                {"kind": kind, "target": target, "id": active.get("id"),
                 "name": active.get("name")}
                if active
                else None
            )
    return managed
