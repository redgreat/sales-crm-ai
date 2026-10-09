"""后台可见的密钥项定义（分组、标签、输入提示）。

所有密钥都随连接存库（admin_connections.credential）、由各连接页维护；
概览只暴露「是否已配置」，明文永不回显。
"""
from __future__ import annotations

from typing import Any

SECRET_PATHS: tuple[str, ...] = (
    "model.api_key",
    "crm.secret",
    "asr.api_key",
    "ocr.access_key_id",
    "ocr.access_key_secret",
    "oss.access_key_id",
    "oss.access_key_secret",
    "research.bocha.api_key",
    "research.qichacha.api_key",
)
SECRET_PATH_SET = frozenset(SECRET_PATHS)

# 前端密钥提示的唯一真源：(路径, 分组, 标签, 提示)
CREDENTIAL_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("model.api_key", "model", "模型 API Key", "随模型服务连接填写"),
    ("crm.secret", "crm", "CRM 签名密钥", "随外部接口（CRM）连接填写"),
    ("asr.api_key", "asr", "ASR API Key", "随外部接口（语音识别）连接填写"),
    ("ocr.access_key_id", "ocr", "OCR AccessKey ID", "随外部接口（文字识别）连接填写"),
    ("ocr.access_key_secret", "ocr", "OCR AccessKey Secret", "随外部接口（文字识别）连接填写"),
    ("oss.access_key_id", "oss", "OSS AccessKey ID", "随外部接口（对象存储）连接填写"),
    ("oss.access_key_secret", "oss", "OSS AccessKey Secret", "随外部接口（对象存储）连接填写"),
    ("research.bocha.api_key", "research", "博查 MCP API Key", "随 MCP 连接填写"),
    ("research.qichacha.api_key", "research", "企查查 MCP API Key", "随 MCP 连接填写"),
)


def credential_specs() -> list[dict[str, Any]]:
    return [
        {"path": path, "group": group, "label": label, "hint": hint}
        for path, group, label, hint in CREDENTIAL_SPECS
    ]
