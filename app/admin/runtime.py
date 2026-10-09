"""运行时映射：把库里启用的连接映射回 Settings 期望的结构。

`apply_records(data, records)` 是唯一的真相合并点：
- 某 (kind, target) 分类有启用记录 → 用该记录的字段与凭据覆盖对应配置段
- 分类在库里有记录但都没启用 → 该分类对应配置段置为禁用（列表接管该段）

凭据落点由 SECRET_TARGETS 决定（代码内契约，不再暴露 secret_path）。

调用方：服务启动时（DB 就绪后）与后台写操作后（热生效）。
"""
from __future__ import annotations

from typing import Any

from app.admin.schema import KINDS, KIND_TARGETS, TARGET_FIELDS

# (kind, target) → {凭据字段: Settings 密钥落点}
SECRET_TARGETS: dict[tuple[str, str], dict[str, str]] = {
    ("model", "model"): {"secret": "model.api_key"},
    ("external", "crm"): {"secret": "crm.secret"},
    ("external", "ocr"): {
        "access_key_id": "ocr.access_key_id",
        "access_key_secret": "ocr.access_key_secret",
    },
    ("external", "asr"): {"api_key": "asr.api_key"},
    ("external", "oss"): {
        "access_key_id": "oss.access_key_id",
        "access_key_secret": "oss.access_key_secret",
    },
    ("mcp", "bocha"): {"secret": "research.bocha.api_key"},
    ("mcp", "qichacha"): {"secret": "research.qichacha.api_key"},
}


def secret_paths(kind: str, target: str) -> dict[str, str]:
    """该分类的凭据字段在 Settings 里的落点（内部契约，不给前端）。"""
    return SECRET_TARGETS.get((kind, target), {})


def apply_records(data: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """把连接记录写回 data（原地修改并返回）。无记录时保持原配置不变。"""
    for kind in KINDS:
        for target in KIND_TARGETS[kind]:
            scoped = [row for row in records
                      if row.get("kind") == kind and row.get("target") == target]
            if not scoped:
                continue
            active = next((row for row in scoped if row.get("enabled")), None)
            if active is None:
                _set_target_disabled(data, kind, target)
                continue
            config = dict(active.get("config") or {})
            credential = dict(active.get("credential") or {})
            if kind == "model":
                # Settings.model 的模型名字段叫 name，连接字段叫 model
                _apply_section(data, "model",
                               {"name" if k == "model" else k: v for k, v in config.items()})
            elif kind == "external":
                _apply_external(data, target, config)
            else:
                _apply_mcp(data, target, config)
            _apply_secret(data, kind, target, credential)
    return data


def _apply_section(data: dict[str, Any], section_key: str, config: dict[str, Any],
                   enabled: bool | None = None) -> dict[str, Any]:
    section = data.setdefault(section_key, {})
    if not isinstance(section, dict):
        return {}
    for field, value in config.items():
        if value is not None:
            section[field] = value
    if enabled is not None:
        section["enabled"] = enabled
    return section


def _apply_external(data: dict[str, Any], target: str, config: dict[str, Any]) -> None:
    if target == "crm":
        # CRM 段没有 enabled 开关：字段映射即接管
        _apply_section(data, "crm", {k: v for k, v in config.items() if k != "enabled"})
        return
    section_key = {"ocr": "ocr", "asr": "asr", "oss": "oss"}[target]
    section = _apply_section(data, section_key, config, enabled=True)
    # 语言提示等列表字段容错：字符串按逗号拆
    if section_key == "asr" and isinstance(section.get("language_hints"), str):
        section["language_hints"] = [
            part.strip() for part in section["language_hints"].split(",") if part.strip()
        ]


def _apply_mcp(data: dict[str, Any], target: str, config: dict[str, Any]) -> None:
    research = data.setdefault("research", {})
    if not isinstance(research, dict):
        return
    section = research.setdefault(target, {})
    if not isinstance(section, dict):
        return
    for field in TARGET_FIELDS[target]:
        if field in config and config[field] is not None:
            section[field] = config[field]
    section["enabled"] = True


def _set_target_disabled(data: dict[str, Any], kind: str, target: str) -> None:
    """分类有记录但全部停用：对应配置段置为禁用（该段已由后台接管）。"""
    if kind == "mcp":
        research = data.setdefault("research", {})
        if isinstance(research, dict):
            section = research.setdefault(target, {})
            if isinstance(section, dict):
                section["enabled"] = False
    elif kind == "external" and target in ("ocr", "asr", "oss"):
        section = data.setdefault(target, {})
        if isinstance(section, dict):
            section["enabled"] = False
    # model / crm 无 enabled 开关，停用即不覆盖基础配置


def _apply_secret(data: dict[str, Any], kind: str, target: str,
                  credential: dict[str, Any]) -> None:
    paths = secret_paths(kind, target)
    for key, value in credential.items():
        dotted = paths.get(key)
        if not dotted or not value:
            continue
        cursor = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = value
