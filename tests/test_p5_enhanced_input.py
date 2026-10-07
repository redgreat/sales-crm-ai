"""P5 增强输入适配层测试（需求 4.12 录音 / 4.13 图片）。

不联网：ASR 用 httpx.MockTransport 注入完整 HTTP 状态机；OCR/OSS 用 Stub 与
配置断言验证。真实凭据到位后的联调属 POC，另行记录（缺凭据不宣称完成）。

覆盖：
- 未启用一律返回 None，不静默兜底；
- 显式启用但凭据缺失直接拒绝启动；
- 异步三步骤（提交→轮询→下载）状态机、失败与超时；
- 时间锚点与原图锚点装配；
- 说话人只区分不映射员工。
"""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings, SettingsError
from app.integrations.asr import (
    AsrError,
    DashScopeAsrClient,
    Transcript,
    build_asr_client,
    format_timestamp,
)
from app.integrations.ocr import (
    AliyunOcrClient,
    OcrError,
    OcrRegion,
    OcrResult,
    build_ocr_client,
)
from app.integrations.oss import OssError, StubOssClient, build_oss_client

_SUBMIT_PATH = "/api/v1/services/audio/asr/transcription"
_TASK_PATH = "/api/v1/tasks/t1"
_RESULT_URL = "https://result.oss-cn-beijing.aliyuncs.com/x.json"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "test",
        "database_url": "postgresql://postgres@127.0.0.1:1/x",
        "model": {"provider": "stub"},
        "auth": {"service_secret": "test-secret"},
    }
    base.update(overrides)
    return Settings(**base)


def _asr_settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {"enabled": True, "api_key": "test-key", "poll_interval_seconds": 0.01}
    payload.update(overrides)
    return _settings(asr=payload)


def _result_payload() -> dict:
    return {
        "transcripts": [
            {
                "text": "客户表示续约价格需要再谈。",
                "sentences": [
                    {"text": "客户表示续约价格需要再谈。", "begin_time": 1200, "end_time": 4800, "speaker_id": "0"}
                ],
            }
        ],
        "properties": {"original_duration_in_milliseconds": 4800},
    }


def _mock_asr_handler(task_statuses: list[str]) -> httpx.MockTransport:
    """按顺序回放任务状态；成功后指向结果 JSON。"""
    calls = {"task": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if _SUBMIT_PATH in url:
            return httpx.Response(200, json={"output": {"task_id": "t1", "task_status": "PENDING"}})
        if _TASK_PATH in url:
            index = calls["task"]
            calls["task"] += 1
            status = task_statuses[min(index, len(task_statuses) - 1)]
            if status == "SUCCEEDED":
                return httpx.Response(
                    200,
                    json={"output": {"task_status": status, "results": [{"transcription_url": _RESULT_URL}]}},
                )
            return httpx.Response(200, json={"output": {"task_status": status}})
        if url.startswith("https://result.oss-cn-beijing.aliyuncs.com/"):
            return httpx.Response(200, json=_result_payload())
        return httpx.Response(404, json={"error": "unexpected"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- 配置与启用


def test_default_all_disabled_returns_none():
    settings = _settings()
    assert build_asr_client(settings) is None
    assert build_ocr_client(settings) is None
    assert build_oss_client(settings) is None


def test_asr_enabled_without_api_key_rejected():
    with pytest.raises(SettingsError, match="asr.api_key"):
        _settings(asr={"enabled": True})


def test_ocr_enabled_without_access_key_rejected():
    with pytest.raises(SettingsError, match="ocr.access_key_id"):
        _settings(ocr={"enabled": True})


def test_oss_enabled_without_endpoint_rejected():
    with pytest.raises(SettingsError, match="oss.endpoint"):
        _settings(oss={"enabled": True})


def test_asr_endpoint_uses_workspace_when_provided():
    settings = _asr_settings(workspace_id="ws-1")
    assert settings.asr.endpoint() == "https://ws-1.cn-beijing.maas.aliyuncs.com"
    assert _asr_settings().asr.endpoint() == "https://dashscope.aliyuncs.com"


def test_env_overrides_inject_secrets_without_file(monkeypatch):
    """密钥可完全不落盘：仅靠环境变量即可通过启用校验。"""
    monkeypatch.setenv("SAI_ASR_API_KEY", "env-asr-key")
    monkeypatch.setenv("SAI_OCR_ACCESS_KEY_ID", "env-ak")
    monkeypatch.setenv("SAI_OCR_ACCESS_KEY_SECRET", "env-sk")
    settings = _settings(asr={"enabled": True}, ocr={"enabled": True})
    assert settings.asr.api_key == "env-asr-key"
    assert settings.ocr.access_key_id == "env-ak"
    assert settings.ocr.access_key_secret == "env-sk"


def test_env_overrides_win_over_config_file(monkeypatch):
    """环境变量优先级高于配置文件：文件里留占位符、部署时注入真值。"""
    monkeypatch.setenv("SAI_ASR_API_KEY", "real-key")
    settings = _settings(asr={"enabled": True, "api_key": "占位符-请替换"})
    assert settings.asr.api_key == "real-key"


def test_crm_and_model_env_overrides(monkeypatch):
    """crm.secret 与 model.api_key 支持环境变量注入（容器部署密钥不落盘）。"""
    monkeypatch.setenv("SAI_CRM_SECRET", "env-crm-secret")
    monkeypatch.setenv("SAI_MODEL_API_KEY", "env-model-key")
    settings = _settings(
        crm={"base_url": "http://127.0.0.1:8080/api/v1/salescrm", "secret": "占位符-请替换"},
        model={"provider": "openai_compatible", "base_url": "https://m.example", "name": "m1", "api_key": "占位符-请替换"},
    )
    assert settings.crm.secret == "env-crm-secret"
    assert settings.model.api_key == "env-model-key"


# ---------------------------------------------------------------- ASR


async def test_asr_polls_until_succeeded_and_builds_time_anchor():
    transport = _mock_asr_handler(["PENDING", "SUCCEEDED"])
    client = httpx.AsyncClient(transport=transport)
    asr = DashScopeAsrClient(_asr_settings(), http_client=client)

    result = await asr.transcribe(audio_url="https://audio.example/a.wav")

    assert result.text == "客户表示续约价格需要再谈。"
    assert result.duration_ms == 4800
    assert len(result.segments) == 1
    assert result.segments[0].start_ms == 1200
    # 时间锚点：1200ms → 00:00:01
    assert "[00:00:01]" in result.anchored_text()
    # 说话人只作区分标识，不映射员工
    assert "说话人0" in result.anchored_text()
    assert result.segments[0].speaker_id == "0"


async def test_asr_rejects_empty_audio_url():
    asr = DashScopeAsrClient(_asr_settings(), http_client=httpx.AsyncClient(transport=_mock_asr_handler(["SUCCEEDED"])))
    with pytest.raises(AsrError, match="audio_url 不能为空"):
        await asr.transcribe(audio_url="   ")


async def test_asr_failed_task_raises():
    asr = DashScopeAsrClient(_asr_settings(), http_client=httpx.AsyncClient(transport=_mock_asr_handler(["FAILED"])))
    with pytest.raises(AsrError, match="识别任务失败"):
        await asr.transcribe(audio_url="https://audio.example/a.wav")


async def test_asr_poll_timeout_raises():
    # 始终 PENDING + 极短超时 → 必须超时失败，而不是返回空结果
    asr = DashScopeAsrClient(
        _asr_settings(poll_timeout_seconds=0.02, poll_interval_seconds=0.01),
        http_client=httpx.AsyncClient(transport=_mock_asr_handler(["PENDING"])),
    )
    with pytest.raises(AsrError, match="轮询超时"):
        await asr.transcribe(audio_url="https://audio.example/a.wav")


async def test_asr_submit_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    asr = DashScopeAsrClient(_asr_settings(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AsrError, match="提交识别任务失败"):
        await asr.transcribe(audio_url="https://audio.example/a.wav")


async def test_asr_missing_transcripts_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if _SUBMIT_PATH in url:
            return httpx.Response(200, json={"output": {"task_id": "t1"}})
        if _TASK_PATH in url:
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", "results": [{"transcription_url": _RESULT_URL}]}})
        return httpx.Response(200, json={"transcripts": []})

    asr = DashScopeAsrClient(_asr_settings(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AsrError, match="缺少 transcripts"):
        await asr.transcribe(audio_url="https://audio.example/a.wav")


def test_format_timestamp():
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(1_200) == "00:00:01"
    assert format_timestamp(3_661_000) == "01:01:01"
    assert format_timestamp(-5) == "00:00:00"


# ---------------------------------------------------------------- OCR


async def test_ocr_requires_exactly_one_image_input():
    settings = _settings(ocr={"enabled": True, "access_key_id": "ak", "access_key_secret": "sk"})
    client = AliyunOcrClient(settings)
    with pytest.raises(OcrError, match="二选一"):
        await client.recognize()
    with pytest.raises(OcrError, match="二选一"):
        await client.recognize(image_url="https://img/x.png", image_bytes=b"x")


def test_ocr_anchor_text_includes_coordinates():
    result = OcrResult(
        text="满 50 万减 2 万",
        regions=(OcrRegion(text="满 50 万减 2 万", points=((10, 20), (300, 20), (300, 60), (10, 60))),),
    )
    rendered = result.anchored_text()
    assert "<R1@10,20,300,20,300,60,10,60>" in rendered
    assert "满 50 万减 2 万" in rendered


def test_ocr_anchor_text_without_coordinates_still_identifiable():
    result = OcrResult(text="无坐标文本", regions=(OcrRegion(text="无坐标文本"),))
    assert "<R1>" in result.anchored_text()


def test_ocr_build_returns_client_when_enabled():
    settings = _settings(ocr={"enabled": True, "access_key_id": "ak", "access_key_secret": "sk"})
    assert build_ocr_client(settings) is not None


# ---------------------------------------------------------------- OSS


async def test_oss_stub_returns_placeholder_url():
    client = StubOssClient()
    url = await client.upload(key="audio/a.wav", data=b"x")
    assert url == "stub://oss/audio/a.wav"


async def test_oss_rejects_empty_key():
    settings = _settings(
        oss={
            "enabled": True,
            "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
            "bucket": "b",
            "access_key_id": "ak",
            "access_key_secret": "sk",
        }
    )
    client = build_oss_client(settings)
    assert client is not None
    with pytest.raises(OssError, match="key"):
        await client.upload(key="", data=b"x")
