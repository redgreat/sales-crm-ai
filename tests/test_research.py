from types import SimpleNamespace

import pytest

from app.api.routes.research import CompanyResearchBody, research_company
from app.config import ResearchMcpProvider, Settings, SettingsError


def test_research_disabled_by_default():
    settings = Settings(environment="test")
    assert not settings.research.bocha.enabled
    assert not settings.research.qichacha.enabled


def test_enabled_research_requires_complete_https_config():
    with pytest.raises(SettingsError):
        Settings(environment="test", research={"bocha": {"enabled": True, "url": "http://localhost/mcp"}})


@pytest.mark.asyncio
async def test_research_returns_unverified_evidence_without_write(monkeypatch):
    settings = Settings(environment="test", research={"bocha": {
        "enabled": True, "url": "https://example.com/mcp", "api_key": "test-key", "tool_name": "web_search",
    }})

    async def fake_search(provider, name):
        assert provider.tool_name == "web_search"
        assert name == "测试公司"
        return {"text": "公开资料", "sources": ["https://example.com/company"], "verified": False}

    monkeypatch.setattr("app.api.routes.research.research_company_mcp", fake_search)
    result = await research_company(CompanyResearchBody(company_name="测试公司", provider="bocha"),
                                    SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings))))
    assert result["verified"] is False
    assert result["sources"] == ["https://example.com/company"]
    assert "未写入 CRM" in result["notice"]
