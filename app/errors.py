"""错误契约：所有业务错误返回统一 JSON。

{"error": {"code": "...", "message": "...", "details": {...}}}
"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    status_code = 500
    code = "INTERNAL"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class Unauthenticated(ApiError):
    status_code = 401
    code = "UNAUTHENTICATED"


class Forbidden(ApiError):
    status_code = 403
    code = "FORBIDDEN"


class NotFound(ApiError):
    status_code = 404
    code = "NOT_FOUND"


class Conflict(ApiError):
    status_code = 409
    code = "CONFLICT"


class RunNotResumable(Conflict):
    code = "RUN_NOT_RESUMABLE"


class StateVersionConflict(Conflict):
    code = "STATE_VERSION_CONFLICT"


class ValidationFailed(ApiError):
    status_code = 422
    code = "VALIDATION_FAILED"


class PayloadTooLarge(ValidationFailed):
    """入参超限：与"格式不对"区分，便于客户端提示"内容过长"而不是重填。"""

    status_code = 413
    code = "PAYLOAD_TOO_LARGE"


class CapabilityUnknown(ApiError):
    status_code = 400
    code = "CAPABILITY_UNKNOWN"


class SchemaNotReady(ApiError):
    status_code = 503
    code = "SCHEMA_NOT_READY"
