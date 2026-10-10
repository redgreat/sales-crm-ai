"""连接的结构定义与校验（与存储无关，文件层和数据库层共用）。

连接按 kind 分菜单、按 target 分「分类」；代码用 (kind, target) 定位配置段：
- model    → target=model，驱动 Settings.model
- external → target ∈ {crm, ocr, asr, oss}，驱动 Settings 对应段（外部接口的分类）
- mcp      → target ∈ {bocha, qichacha}，驱动 Settings.research.<target>

互斥规则：同一 (kind, target) 分类下有且只能有一条启用连接（默认值），
由 partial unique index 兜底、应用层在写入时先关同分类旧记录。
"""
from __future__ import annotations

from typing import Any

from app.errors import ValidationFailed

KINDS = ("model", "external", "mcp")
KIND_TARGETS: dict[str, tuple[str, ...]] = {
    "model": ("model",),
    "external": ("crm", "ocr", "asr", "oss"),
    "mcp": ("bocha", "qichacha"),
}
KIND_LABELS: dict[str, str] = {"model": "模型服务", "external": "外部接口", "mcp": "MCP 服务"}

# 分类（target）展示名：前端分类下拉与列表直接使用
TARGET_LABELS: dict[str, str] = {
    "model": "默认模型",
    "crm": "CRM 接口",
    "ocr": "OCR 文字识别",
    "asr": "ASR 语音识别",
    "oss": "OSS 对象存储",
    "bocha": "博查",
    "qichacha": "企查查",
}

# 每个分类的业务字段白名单（id/name/enabled 等通用字段另行处理）
TARGET_FIELDS: dict[str, tuple[str, ...]] = {
    "model": ("provider", "base_url", "model", "enable_thinking", "temperature",
              "timeout_seconds", "max_retries"),
    "crm": ("base_url", "key_id", "timeout_seconds"),
    "ocr": ("endpoint", "type", "output_coordinate", "timeout_seconds"),
    "asr": ("model", "language_hints", "diarization_enabled"),
    "oss": ("endpoint", "bucket", "signed_url_ttl_seconds"),
    "bocha": ("url", "tool_name", "query_argument", "timeout_seconds"),
    "qichacha": ("url", "tool_name", "query_argument", "timeout_seconds"),
}

# 字段类型（用于输入强转；缺省按字符串处理）
FIELD_TYPES: dict[str, str] = {
    "temperature": "number",
    "timeout_seconds": "number",
    "max_retries": "number",
    "signed_url_ttl_seconds": "number",
    "language_hints": "list",
    "diarization_enabled": "bool",
    "enable_thinking": "bool",
}

# 每个分类的凭据字段（写入 credential JSONB，只写不回显）
TARGET_SECRETS: dict[str, tuple[str, ...]] = {
    "model": ("secret",),
    "crm": ("secret",),
    "ocr": ("access_key_id", "access_key_secret"),
    "asr": ("api_key",),
    "oss": ("access_key_id", "access_key_secret"),
    "bocha": ("secret",),
    "qichacha": ("secret",),
}

# 允许从请求体写入的字段（其余一律拒绝，避免后台被当成任意配置写入口）
WRITE_FIELDS = ("name", "provider", "base_url", "model", "url", "tool_name", "query_argument",
                "enable_thinking", "temperature", "timeout_seconds", "max_retries", "key_id",
                "endpoint", "type", "output_coordinate", "language_hints", "diarization_enabled",
                "bucket", "signed_url_ttl_seconds", "target", "enabled", "secrets")


def _target_of(kind: str, payload: dict[str, Any], existing: dict[str, Any] | None) -> str:
    allowed = KIND_TARGETS[kind]
    target = payload.get("target") if isinstance(payload, dict) else None
    if target is None and existing:
        target = existing.get("target")
    if len(allowed) == 1 and target is None:
        return allowed[0]
    if target not in allowed:
        raise ValidationFailed(
            f"{KIND_LABELS[kind]}连接的分类必须是 {', '.join(allowed)} 之一")
    return str(target)


def resolve_target(kind: str, payload: dict[str, Any],
                   existing: dict[str, Any] | None = None) -> str:
    return _target_of(kind, payload, existing)


def split_secrets(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """拆出随连接提交的凭据：不属于业务字段，单独写入凭据列，且永不回显。

    兼容旧字段 `secret`（单凭据分类的简写）；多凭据分类用 `secrets` 对象。
    """
    body = dict(payload)
    collected: dict[str, str] = {}
    legacy = body.pop("secret", None)
    if isinstance(legacy, str) and legacy.strip():
        collected["secret"] = legacy.strip()
    secrets = body.pop("secrets", None)
    if isinstance(secrets, dict):
        for key, value in secrets.items():
            if isinstance(value, str) and value.strip():
                collected[str(key)] = value.strip()
    return body, collected


def _coerce_value(field: str, value: Any) -> Any:
    ftype = FIELD_TYPES.get(field)
    if value is None:
        return None
    if ftype == "number":
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if ftype == "list":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        if not text:
            return []
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


def check_secrets(target: str, secrets: dict[str, str]) -> dict[str, str]:
    allowed = TARGET_SECRETS[target]
    unexpected = sorted(set(secrets) - set(allowed))
    if unexpected:
        raise ValidationFailed(f"不支持的凭据字段：{', '.join(unexpected)}")
    return {key: secrets[key] for key in allowed if secrets.get(key)}


def coerce_fields(payload: dict[str, Any], kind: str, target: str,
                  existing: dict[str, Any] | None) -> dict[str, Any]:
    """校验并合并业务字段；返回 {业务字段..., name, enabled}。"""
    known = set(TARGET_FIELDS[target]) | set(WRITE_FIELDS)
    unexpected = sorted(set(payload) - known)
    if unexpected:
        raise ValidationFailed(f"包含不支持的字段：{', '.join(unexpected)}")
    record: dict[str, Any] = dict(existing) if existing else {}
    record["target"] = target
    for field in TARGET_FIELDS[target]:
        if field in payload:
            record[field] = _coerce_value(field, payload[field])
    if "name" in payload:
        record["name"] = str(payload["name"]).strip() or (existing or {}).get("name", "未命名")
    if "enabled" in payload:
        record["enabled"] = bool(payload["enabled"])
    # 保存即启用：新建时未显式给 enabled 一律视为启用
    record.setdefault("enabled", True)
    if not record.get("name"):
        raise ValidationFailed("连接名称不能为空")
    return record


def check_kind(kind: str) -> str:
    from app.errors import NotFound

    if kind not in KINDS:
        raise NotFound(f"不支持的连接类型：{kind}")
    return kind
