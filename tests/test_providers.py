"""Stub Provider 与 usage 剥离测试。"""
from __future__ import annotations

import json

from app.config import Settings
from app.providers.factory import build_chat_model
from app.providers.stub import StubChatModel, parse_user_text, strip_usage_metadata


def test_openai_compatible_defaults_enable_thinking_false():
    """百炼深度思考默认关闭，经 extra_body 传出。"""
    settings = Settings(
        model={
            "provider": "openai_compatible",
            "base_url": "https://example.maas.aliyuncs.com/compatible-mode/v1",
            "api_key": "k",
            "name": "qwen-test",
        },
    )
    assert settings.model.enable_thinking is False
    model = build_chat_model(settings)
    assert model.extra_body == {"enable_thinking": False}


def test_openai_compatible_omits_enable_thinking_when_null():
    settings = Settings(
        model={
            "provider": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "api_key": "k",
            "name": "m",
            "enable_thinking": None,
        },
    )
    model = build_chat_model(settings)
    assert not model.extra_body


def test_parse_full_text():
    payload = parse_user_text("客户：ACME公司\n任务：拜访客户\n负责人：张三\n2026-10-01 开会")
    assert payload["customers"] == ["ACME公司"]
    assert payload["tasks"][0]["title"] == "拜访客户"
    assert payload["tasks"][0]["due_date"] == "2026-10-01"
    assert payload["tasks"][0]["assignee_name"] == "张三"
    assert payload["missing"] == []


def test_parse_missing_fields():
    payload = parse_user_text("[[缺日期]]\n任务：电话跟进")
    task = payload["tasks"][0]
    assert "due_date" not in task
    missing_fields = {m["field"] for m in payload["missing"]}
    assert missing_fields == {"customer_name", "due_date", "assignee_name"}


async def test_stub_model_outputs_json_without_usage():
    from langchain_core.messages import HumanMessage

    model = StubChatModel()
    prompt = "prompt 前缀 <<USER_TEXT>>\n客户：ACME\n任务：回访\n2026-10-02\n<<END_USER_TEXT>>"
    response = await model.ainvoke([HumanMessage(content=prompt)])
    payload = json.loads(response.content)
    assert payload["customers"] == ["ACME"]
    # 不携带 usage/token 元数据（字段可为 schema 默认空值，但不得有数据）
    dump = response.model_dump()
    assert not dump.get("usage_metadata")
    assert not dump.get("response_metadata")
    assert not dump.get("additional_kwargs")


def test_strip_usage_metadata_recursive():
    dirty = {
        "a": 1,
        "usage_metadata": {"input_tokens": 10},
        "response_metadata": {"model": "x"},
        "token_usage": {"total": 3},
        "nested": [{"cost_usd": 1.5, "keep": True}],
    }
    clean = strip_usage_metadata(dirty)
    assert clean == {"a": 1, "nested": [{"keep": True}]}
