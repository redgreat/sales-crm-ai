"""P1 加固回归：结果版本透出、输入上限、错误脱敏、MCP 出站守卫。

这些用例不依赖真实 PostgreSQL（`conftest` 会自动跳过需要 PG 的部分），
目的是让"加固是否还在"可被 CI 自动发现，而不是靠人工复核。
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from app.api.routes.runs import _serialize
from app.config import Settings, SettingsError
from app.errors import PayloadTooLarge
from app.input_limits import (
    MAX_FACTS_ITEMS,
    MAX_INPUT_BYTES,
    validate_resume_values,
    validate_run_input,
)
from app.integrations.research import research_company_mcp
from app.net_guard import OutboundUrlRejected, assert_public_https_url
from app.runtime.executor import Executor
from app.util import redact_error_message, safe_error_message


def _run_row(**overrides):
    row = {
        "run_id": "r-1",
        "capability": "business.qa",
        "status": "succeeded",
        "idempotency_key": "k",
        "conversation_id": None,
        "attempt_count": 1,
        "max_attempts": 3,
        "graph_version": "analysis-qa@1",
        "prompt_version": "p1",
        "result_version": 2,
        "result": {"summary": "x"},
        "error": None,
        "status_history": [],
    }
    row.update(overrides)
    return row


# --- 结果版本透出 -----------------------------------------------------------


def test_serialize_exposes_result_version():
    payload = _serialize(_run_row(result_version=3))
    assert payload["result_version"] == 3


def test_serialize_keeps_result_version_null_before_completion():
    payload = _serialize(_run_row(status="queued", result_version=0, result=None))
    assert payload["result_version"] == 0
    assert payload["result"] is None


# --- 输入上限 ---------------------------------------------------------------


def test_input_rejects_oversized_text():
    with pytest.raises(PayloadTooLarge):
        validate_run_input({"text": "甲" * 20001})


def test_input_rejects_too_many_facts():
    facts = [{"id": f"f{i}", "label": "x"} for i in range(MAX_FACTS_ITEMS + 1)]
    with pytest.raises(PayloadTooLarge):
        validate_run_input({"facts": facts})


def test_input_rejects_oversized_payload():
    with pytest.raises(PayloadTooLarge):
        validate_run_input({"blob": "x" * (MAX_INPUT_BYTES + 1)})


def test_input_accepts_normal_payload():
    validate_run_input({"text": "沟通记录", "facts": [{"id": "1", "label": "客户"}]})


def test_resume_values_rejects_oversized():
    with pytest.raises(PayloadTooLarge):
        validate_resume_values({"answer": "x" * 20001})


# --- 错误脱敏 ---------------------------------------------------------------


def test_redact_removes_bearer_and_query_string():
    raw = "HTTP 401 api_key=sk-abcdef123456 token: TTTTTTTTTT https://x.example.com/a?sig=zzz"
    out = redact_error_message(raw)
    assert "sk-abcdef123456" not in out
    assert "TTTTTTTTTT" not in out
    assert "sig=zzz" not in out


def test_safe_error_message_keeps_only_type():
    class _Boom(RuntimeError):
        pass

    assert safe_error_message(_Boom("secret=12345")) == "内部执行错误（_Boom）"


@pytest.mark.asyncio
async def test_handle_failure_sanitizes_before_persist(monkeypatch):
    captured: dict = {}

    async def fake_fail_run(pool, *, run_id, generation, error, backoff_seconds, terminal=False):
        captured.update(error)
        return "failed"

    monkeypatch.setattr("app.runtime.executor.runs_repo.fail_run", fake_fail_run)
    executor = Executor(pool=None, graphs={}, lease_seconds=1)
    await executor._handle_failure(
        "r-1", 1, {"code": "EXECUTOR_CRASH", "message": "api_key=sk-abcdef123456 boom"}
    )
    assert captured["code"] == "EXECUTOR_CRASH"
    assert "sk-abcdef123456" not in captured["message"]


# --- MCP 出站守卫 -----------------------------------------------------------


def test_outbound_rejects_loopback_and_link_local():
    for url in ("https://127.0.0.1/mcp", "https://169.254.169.254/latest/meta-data",
                "http://example.com/mcp", "https://user:pw@example.com/mcp"):
        with pytest.raises(OutboundUrlRejected):
            assert_public_https_url(url, resolve=False)


def test_outbound_accepts_public_https_without_dns():
    assert assert_public_https_url("https://mcp.example.com/mcp", resolve=False).endswith("/mcp")


def test_outbound_enforces_host_allowlist():
    with pytest.raises(OutboundUrlRejected):
        assert_public_https_url(
            "https://evil.example.com/mcp", allowed_hosts=["mcp.example.com"], resolve=False
        )


def test_outbound_rejects_hostname_resolving_to_private_ip(monkeypatch):
    def fake_getaddrinfo(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr("app.net_guard.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(OutboundUrlRejected):
        assert_public_https_url("https://internal.example.com/mcp")


@pytest.mark.asyncio
async def test_research_guards_url_before_connecting(monkeypatch):
    async def fail_if_called(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("不应向被拒绝的地址发起连接")

    monkeypatch.setattr("app.integrations.research.streamablehttp_client", fail_if_called)
    from app.config import ResearchMcpProvider

    provider = ResearchMcpProvider(
        enabled=True, url="https://192.168.1.10/mcp", api_key="k", tool_name="t"
    )
    with pytest.raises(OutboundUrlRejected):
        await research_company_mcp(provider, "测试公司")


def test_settings_rejects_private_research_url():
    with pytest.raises(SettingsError):
        Settings(environment="test", research={"bocha": {
            "enabled": True, "url": "https://10.0.0.5/mcp", "api_key": "k", "tool_name": "t",
        }})


def test_settings_rejects_research_url_outside_allowlist():
    with pytest.raises(SettingsError):
        Settings(environment="test", research={"bocha": {
            "enabled": True, "url": "https://other.example.com/mcp", "api_key": "k",
            "tool_name": "t", "allowed_hosts": ["mcp.example.com"],
        }})
