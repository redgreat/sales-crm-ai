"""通用工具：UTC 时间与 ID，以及错误信息脱敏。"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

# 错误信息会落库（ai_runs.error / status_history）并原样返回给调用方。
# 供应商/驱动异常的文本常包含完整请求体、密钥与内网地址，不能直接透出。
_REDACTION_LIMIT = 200
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?key(?:[_-]?secret)?|secret|token|password|passwd|"
    r"authorization|bearer|signature|sign|cookie|session)\b\s*[:=]?\s*[\"']?"
    r"([^\s,;\"'}\]]{4,})"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{6,}")
_JSON_WEB_TOKEN = re.compile(r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}(?:\.[A-Za-z0-9_\-]*)?")
_VENDOR_KEY = re.compile(r"\b(?:sk|ak)-[A-Za-z0-9]{8,}")
_URL_QUERY = re.compile(r"(?i)\b(?:https?://[^\s]+)\?[^\s]+")


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def redact_error_message(message: str, *, limit: int = _REDACTION_LIMIT) -> str:
    """把异常文本收敛为可落库/可返回的短文本，剥离凭据与带查询串的 URL。

    只做"去掉明显敏感片段 + 截断"的保守处理，不试图解析供应商报文结构；
    因此未识别的高熵内容仍可能被保留——涉及未知异常类型时应优先只回错误码。
    """
    text = " ".join(str(message or "").split())
    if not text:
        return ""
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _JSON_WEB_TOKEN.sub("[REDACTED]", text)
    text = _VENDOR_KEY.sub("[REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: m.group(0).replace(m.group(1), "[REDACTED]"), text)
    text = _URL_QUERY.sub(lambda m: m.group(0).split("?", 1)[0] + "?[REDACTED]", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def safe_error_message(exc: BaseException, *, limit: int = _REDACTION_LIMIT) -> str:
    """未知异常的落库文案：保留类型名（可定位），不回显原始报文。"""
    return f"内部执行错误（{type(exc).__name__}）"[:limit]
