"""Human-triggered external research. Never writes CRM facts or AI draft fields."""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.errors import ValidationFailed
from app.integrations.research import research_company_mcp

router = APIRouter(prefix="/api/v1/research", tags=["research"])
logger = logging.getLogger("sales-crm-ai.research")


class CompanyResearchBody(BaseModel):
    company_name: str = Field(min_length=2, max_length=100)
    provider: Literal["bocha", "qichacha"]


@router.post("/company")
async def research_company(body: CompanyResearchBody, request: Request) -> dict[str, Any]:
    provider = getattr(request.app.state.settings.research, body.provider)
    if not provider.enabled:
        raise ValidationFailed(f"{body.provider} MCP 未启用")
    name = body.company_name.strip()
    if len(name) < 2:
        raise ValidationFailed("企业名称至少 2 个字符")
    try:
        result = await research_company_mcp(provider, name)
    except Exception as exc:
        logger.warning("企业外部查询失败: provider=%s error_type=%s", body.provider, type(exc).__name__)
        raise ValidationFailed("外部查询失败，请检查 MCP 服务配置或稍后重试") from None
    return {"provider": body.provider, "company_name": name, **result,
            "notice": "仅供人工核实的外部线索，未写入 CRM；联系人和号码须核对来源及使用授权。"}
