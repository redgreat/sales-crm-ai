"""CRM 事实查询与附件受控访问客户端。

签名方案见 app/auth.py；当前 P1-P3 阶段图输入由 CRM 装配后随 Run 传入，
此客户端供 P4 闭环（对象摘要/日报等按需回查）与 P5 文件边界
（file_ref 受控访问，需求 12.3：持久化文件标识而非签名 URL，过期重新鉴权）使用，
并配 Stub 供联调。只有只读 GET：正式写入永远在 CRM 侧。
"""
from __future__ import annotations

import re
from typing import Any, Protocol

import httpx

from app.auth import OperatorContext, sign_request
from app.config import Settings
from urllib.parse import urlparse

_FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _clean_file_id(file_id: str) -> str:
    """路径注入防护：CRM 附件 id 是 RS+10 位数字，这里按白名单字符收口。"""
    cleaned = str(file_id or "").strip()
    if not _FILE_ID_PATTERN.fullmatch(cleaned):
        raise ValueError(f"非法附件 id: {file_id!r}")
    return cleaned


class CrmFactsClient(Protocol):
    async def query_object_facts(
        self, *, operator: OperatorContext, subject_type: str, subject_id: str
    ) -> dict[str, Any]: ...

    async def get_integration_file(
        self, *, operator: OperatorContext, file_id: str
    ) -> dict[str, Any]: ...

    async def download_integration_file(
        self, *, operator: OperatorContext, file_id: str
    ) -> bytes: ...


class HttpCrmFactsClient:
    """真实 CRM 客户端：GET {base_url}/ai/integration/facts/{subject_type}/{subject_id}。

    签名 path 口径与 CRM 侧 AiServiceSignatureFilter 验签一致（request.getRequestURI，
    含应用前缀）：base_url 形如 http://host:8080/api/v1/salescrm 时，签名串里的 path
    是 /api/v1/salescrm/ai/integration/facts/...，而不是去掉前缀的相对路径。
    """

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self._settings = settings
        self._path_prefix = urlparse(str(settings.crm.base_url)).path.rstrip("/")
        self._client = http_client or httpx.AsyncClient(
            base_url=settings.crm.base_url,
            timeout=settings.crm.timeout_seconds,
        )

    async def query_object_facts(
        self, *, operator: OperatorContext, subject_type: str, subject_id: str
    ) -> dict[str, Any]:
        relative_path = f"/ai/integration/facts/{subject_type}/{subject_id}"
        signed_path = f"{self._path_prefix}{relative_path}"
        headers = sign_request(
            secret=self._settings.crm.secret,
            key_id=self._settings.crm.key_id,
            method="GET",
            path=signed_path,
            operator=operator,
        )
        response = await self._client.get(relative_path, headers=headers)
        response.raise_for_status()
        return response.json()

    async def get_integration_file(
        self, *, operator: OperatorContext, file_id: str
    ) -> dict[str, Any]:
        """附件元数据与受控访问方式（需求 12.5 契约）。

        响应：{found, file:{...}, access:{type: signed_url|inline|none, url?, expires_in_seconds?}}。
        非本人/不存在/已删除由 CRM 归一 found=false；found=true 且 status=REVOKED 时
        access.type=none。签名 URL 短时有效，过期后重新调用本方法获取。
        """
        relative_path = f"/ai/integration/files/{_clean_file_id(file_id)}"
        signed_path = f"{self._path_prefix}{relative_path}"
        headers = sign_request(
            secret=self._settings.crm.secret,
            key_id=self._settings.crm.key_id,
            method="GET",
            path=signed_path,
            operator=operator,
        )
        response = await self._client.get(relative_path, headers=headers)
        response.raise_for_status()
        return response.json()

    async def download_integration_file(
        self, *, operator: OperatorContext, file_id: str
    ) -> bytes:
        """取附件字节（仅 CRM local 存储模式提供；oss 模式 CRM 返回 400，须用签名 URL）。"""
        relative_path = f"/ai/integration/files/{_clean_file_id(file_id)}/content"
        signed_path = f"{self._path_prefix}{relative_path}"
        headers = sign_request(
            secret=self._settings.crm.secret,
            key_id=self._settings.crm.key_id,
            method="GET",
            path=signed_path,
            operator=operator,
        )
        response = await self._client.get(relative_path, headers=headers)
        response.raise_for_status()
        return response.content

    async def aclose(self) -> None:
        await self._client.aclose()


class StubCrmFactsClient:
    """联调用 Stub：返回固定有权事实，不访问任何真实 CRM。"""

    def __init__(
        self,
        facts: dict[str, dict[str, Any]] | None = None,
        files: dict[str, dict[str, Any]] | None = None,
        file_bytes: dict[str, bytes] | None = None,
    ):
        self._facts = facts or {}
        self._files = files or {}
        self._file_bytes = file_bytes or {}

    async def query_object_facts(
        self, *, operator: OperatorContext, subject_type: str, subject_id: str
    ) -> dict[str, Any]:
        key = f"{subject_type}:{subject_id}"
        facts = self._facts.get(key)
        if facts is None:
            return {"subject_type": subject_type, "subject_id": subject_id, "found": False, "facts": {}}
        return {"subject_type": subject_type, "subject_id": subject_id, "found": True, "facts": facts}

    async def get_integration_file(
        self, *, operator: OperatorContext, file_id: str
    ) -> dict[str, Any]:
        cleaned = _clean_file_id(file_id)
        meta = self._files.get(cleaned)
        if meta is None:
            return {"found": False, "file": None}
        return {
            "found": True,
            "file": meta,
            "access": {
                "type": "inline",
                "content_path": f"/api/v1/salescrm/ai/integration/files/{cleaned}/content",
            },
        }

    async def download_integration_file(
        self, *, operator: OperatorContext, file_id: str
    ) -> bytes:
        cleaned = _clean_file_id(file_id)
        return self._file_bytes.get(cleaned, b"")


def build_crm_client(settings: Settings) -> CrmFactsClient | None:
    if not settings.crm.base_url:
        return None
    if settings.environment == "prod" and not settings.crm.secret:
        raise RuntimeError("生产环境访问 CRM 必须配置 SAI_CRM_SECRET")
    return HttpCrmFactsClient(settings)
