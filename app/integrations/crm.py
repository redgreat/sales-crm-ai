"""CRM 事实查询客户端。

签名方案见 app/auth.py；当前 P1-P3 阶段图输入由 CRM 装配后随 Run 传入，
此客户端供 P4 闭环（对象摘要/日报等按需回查）使用，并配 Stub 供联调。
"""
from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.auth import OperatorContext, sign_request
from app.config import Settings


class CrmFactsClient(Protocol):
    async def query_object_facts(
        self, *, operator: OperatorContext, subject_type: str, subject_id: str
    ) -> dict[str, Any]: ...


class HttpCrmFactsClient:
    """真实 CRM 客户端：GET /ai/integration/facts/{subject_type}/{subject_id}。"""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self._settings = settings
        self._client = http_client or httpx.AsyncClient(
            base_url=settings.crm.base_url,
            timeout=settings.crm.timeout_seconds,
        )

    async def query_object_facts(
        self, *, operator: OperatorContext, subject_type: str, subject_id: str
    ) -> dict[str, Any]:
        path = f"/ai/integration/facts/{subject_type}/{subject_id}"
        headers = sign_request(
            secret=self._settings.crm.secret,
            key_id=self._settings.crm.key_id,
            method="GET",
            path=path,
            operator=operator,
        )
        response = await self._client.get(path, headers=headers)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


class StubCrmFactsClient:
    """联调用 Stub：返回固定有权事实，不访问任何真实 CRM。"""

    def __init__(self, facts: dict[str, dict[str, Any]] | None = None):
        self._facts = facts or {}

    async def query_object_facts(
        self, *, operator: OperatorContext, subject_type: str, subject_id: str
    ) -> dict[str, Any]:
        key = f"{subject_type}:{subject_id}"
        facts = self._facts.get(key)
        if facts is None:
            return {"subject_type": subject_type, "subject_id": subject_id, "found": False, "facts": {}}
        return {"subject_type": subject_type, "subject_id": subject_id, "found": True, "facts": facts}


def build_crm_client(settings: Settings) -> CrmFactsClient | None:
    if not settings.crm.base_url:
        return None
    if settings.environment == "prod" and not settings.crm.secret:
        raise RuntimeError("生产环境访问 CRM 必须配置 SAI_CRM_SECRET")
    return HttpCrmFactsClient(settings)
