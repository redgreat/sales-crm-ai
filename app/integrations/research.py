"""Explicit, read-only enterprise research through configured MCP servers."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared._httpx_utils import create_mcp_http_client

from app.config import ResearchMcpProvider
from app.net_guard import OutboundUrlRejected, assert_public_https_url

_URL = re.compile(r"https?://[^\s<>\"']+")


def _no_redirect_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """禁止跟随重定向：重定向会把 Bearer 凭据带到已校验目标之外。"""
    client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
    client.follow_redirects = False
    return client


async def research_company_mcp(provider: ResearchMcpProvider, company_name: str) -> dict[str, Any]:
    # 出站前校验：凭据只发给通过校验的公网 https 地址（防 SSRF / 内网凭据外泄）
    assert_public_https_url(provider.url, allowed_hosts=provider.allowed_hosts)
    query = f"{company_name} 企业官网 联系方式 联系人 统一社会信用代码"
    async with streamablehttp_client(
        provider.url,
        headers={"Authorization": f"Bearer {provider.api_key}"},
        timeout=provider.timeout_seconds,
        sse_read_timeout=provider.timeout_seconds,
        httpx_client_factory=_no_redirect_client,
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            if provider.tool_name not in {tool.name for tool in tools.tools}:
                raise ValueError("配置的 MCP 工具不存在")
            result = await session.call_tool(
                provider.tool_name,
                {provider.query_argument: query},
                read_timeout_seconds=timedelta(seconds=provider.timeout_seconds),
            )
    if result.isError:
        # 不回显供应商报文（可能含查询串、凭据或内部地址）
        raise ValueError("MCP 查询失败（供应商返回错误）")
    excerpts = [item.text[:4000] for item in result.content if getattr(item, "type", "") == "text"]
    text = "\n".join(excerpts)[:12000]
    sources = list(dict.fromkeys(_URL.findall(text)))[:20]
    return {"text": text, "sources": sources, "verified": False}


__all__ = ["OutboundUrlRejected", "research_company_mcp"]
