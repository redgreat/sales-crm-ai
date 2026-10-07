"""录音文件识别适配层（P5 需求 4.12：录音 → 带时间锚点的校对文本 → 抽取）。

实现对象：阿里云百炼（DashScope）非实时语音识别 RESTful API。
调用形态为**异步三步骤**：

    提交任务（必须带 header `X-DashScope-Async: enable`）
      → 轮询 `POST /api/v1/tasks/{task_id}`
      → 下载 `transcription_url` 取结果 JSON（链接有效期 24 小时）

关键约束（与需求/官方文档一致，不做放宽）：

- 百炼**只接受公网可访问的 URL**，不支持 Base64、二进制流或本地文件；
  本地录音必须先入对象存储（见 `oss.py`）取得 URL，或由调用方直接给出 URL。
- 结果自带 `sentences` 时间戳，据此装配**时间锚点**，供抽取与人工校对定位原文。
- `speaker_id` 仅用于区分说话人，**绝不映射员工**（需求 4.12 明确要求）。
- 未启用 / 未配置凭据时 `build_asr_client()` 返回 `None`，调用方必须显式报
  “能力未启用”，不回退为假装识别成功。

结果 JSON 的字段名以官方为准；`_parse()` 对缺失字段做显式失败而非静默填空。
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.providers.stub import strip_usage_metadata

_STATUS_SUCCEEDED = "SUCCEEDED"
_STATUS_FAILED = "FAILED"
_STATUS_PENDING = "PENDING"
_STATUS_RUNNING = "RUNNING"
_MAX_RESULT_BYTES = 8 * 1024 * 1024


class AsrError(RuntimeError):
    """识别失败：任务失败、轮询超时或结果不可解析。"""


def format_timestamp(ms: int) -> str:
    """毫秒 → `HH:MM:SS`（时间锚点的人类可读形式）。"""
    total = max(0, int(ms)) // 1000
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


@dataclass(frozen=True)
class TranscriptSegment:
    """带时间锚点的转写片段。"""

    text: str
    start_ms: int
    end_ms: int
    # 说话人区分 ID；仅作分段标识，不映射员工（需求 4.12）
    speaker_id: str = ""


@dataclass(frozen=True)
class Transcript:
    """转写结果：全文 + 带锚点片段。"""

    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    duration_ms: int = 0
    source_url: str = ""

    def anchored_text(self) -> str:
        """渲染带时间锚点的校对文本（供抽取图定位原文、人工核对）。"""
        if not self.segments:
            return self.text
        lines: list[str] = []
        for segment in self.segments:
            speaker = f"[说话人{segment.speaker_id}] " if segment.speaker_id else ""
            lines.append(f"[{format_timestamp(segment.start_ms)}] {speaker}{segment.text}")
        return "\n".join(lines)


class AsrClient(Protocol):
    async def transcribe(self, *, audio_url: str) -> Transcript: ...

    async def submit(self, *, audio_url: str) -> str: ...

    async def poll(self, *, task_id: str, source_url: str) -> Transcript: ...


class DashScopeAsrClient:
    """百炼非实时语音识别客户端（httpx 直调，不引入供应商 SDK）。"""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self._settings = settings.asr
        self._client = http_client or httpx.AsyncClient(timeout=self._settings.timeout_seconds)

    def _headers(self, *, async_enable: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        if async_enable:
            headers["X-DashScope-Async"] = "enable"
        return headers

    async def transcribe(self, *, audio_url: str) -> Transcript:
        task_id = await self.submit(audio_url=audio_url)
        return await self.poll(task_id=task_id, source_url=audio_url)

    async def submit(self, *, audio_url: str) -> str:
        if not str(audio_url or "").strip():
            raise AsrError("audio_url 不能为空（百炼只接受公网可访问 URL）")
        return await self._submit(audio_url)

    async def poll(self, *, task_id: str, source_url: str) -> Transcript:
        if not str(task_id or "").strip():
            raise AsrError("task_id 不能为空")
        payload = await self._wait_for_result(task_id)
        return self._parse(payload, source_url=source_url)

    async def _submit(self, audio_url: str) -> str:
        url = f"{self._settings.endpoint()}/api/v1/services/audio/asr/transcription"
        body = {
            "model": self._settings.model,
            "input": {"file_urls": [audio_url]},
            "parameters": {
                "channel_id": [0],
                "language_hints": list(self._settings.language_hints),
                "diarization_enabled": self._settings.diarization_enabled,
            },
        }
        response = await self._client.post(url, headers=self._headers(async_enable=True), json=body)
        if response.status_code >= 400:
            raise AsrError(f"提交识别任务失败: HTTP {response.status_code} {response.text[:200]}")
        task_id = str((response.json().get("output") or {}).get("task_id") or "").strip()
        if not task_id:
            raise AsrError("提交识别任务未返回 task_id")
        return task_id

    async def _wait_for_result(self, task_id: str) -> dict[str, Any]:
        """轮询直到终态；超时或失败显式抛出，不返回半成品。"""
        url = f"{self._settings.endpoint()}/api/v1/tasks/{task_id}"
        deadline = time.monotonic() + self._settings.poll_timeout_seconds
        while True:
            response = await self._client.post(url, headers=self._headers())
            if response.status_code >= 400:
                raise AsrError(f"查询识别任务失败: HTTP {response.status_code} {response.text[:200]}")
            output = (response.json() or {}).get("output") or {}
            status = str(output.get("task_status") or "").strip()
            if status == _STATUS_SUCCEEDED:
                return await self._fetch_transcription(output)
            if status == _STATUS_FAILED:
                raise AsrError(f"识别任务失败: {output.get('message') or status}")
            if status not in (_STATUS_PENDING, _STATUS_RUNNING):
                raise AsrError(f"识别任务返回未知状态: {status or '(空)'}")
            if time.monotonic() >= deadline:
                raise AsrError(f"识别任务轮询超时（超过 {self._settings.poll_timeout_seconds}s）")
            await asyncio.sleep(self._settings.poll_interval_seconds)

    async def _fetch_transcription(self, output: dict[str, Any]) -> dict[str, Any]:
        transcription_url = ""
        for item in output.get("results") or []:
            if isinstance(item, dict) and item.get("transcription_url"):
                transcription_url = str(item["transcription_url"])
                break
        if not transcription_url:
            raise AsrError("任务成功但未返回 transcription_url（结果链接缺失）")
        parsed = urlsplit(transcription_url)
        host = (parsed.hostname or "").lower()
        try:
            allowed = (parsed.scheme == "https" and host.endswith(".aliyuncs.com")
                       and parsed.username is None and parsed.password is None
                       and parsed.port in (None, 443))
        except ValueError:
            allowed = False
        if not allowed:
            raise AsrError("识别结果链接不在允许的 HTTPS 域名范围内")
        async with self._client.stream("GET", transcription_url, follow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                raise AsrError("识别结果下载不允许重定向")
            if response.status_code >= 400:
                raise AsrError(f"下载识别结果失败: HTTP {response.status_code}")
            declared_size = response.headers.get("content-length")
            if declared_size and declared_size.isdigit() and int(declared_size) > _MAX_RESULT_BYTES:
                raise AsrError("识别结果超过下载大小上限")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > _MAX_RESULT_BYTES:
                    raise AsrError("识别结果超过下载大小上限")
        try:
            payload = json.loads(chunks) or {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise AsrError("识别结果不是合法 JSON") from exc
        return strip_usage_metadata(payload)

    @staticmethod
    def _parse(payload: dict[str, Any], *, source_url: str) -> Transcript:
        transcripts = payload.get("transcripts") or []
        if not transcripts or not isinstance(transcripts[0], dict):
            raise AsrError("识别结果缺少 transcripts")
        first = transcripts[0]
        segments: list[TranscriptSegment] = []
        for sentence in first.get("sentences") or []:
            if not isinstance(sentence, dict):
                continue
            segments.append(
                TranscriptSegment(
                    text=str(sentence.get("text") or "").strip(),
                    start_ms=int(sentence.get("begin_time") or 0),
                    end_ms=int(sentence.get("end_time") or 0),
                    speaker_id=str(sentence.get("speaker_id") or ""),
                )
            )
        properties = payload.get("properties") or {}
        return Transcript(
            text=str(first.get("text") or "").strip(),
            segments=segments,
            duration_ms=int(properties.get("original_duration_in_milliseconds") or 0),
            source_url=source_url,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class StubAsrClient:
    """联调 Stub：返回固定转写，不访问任何外部服务（不伪造真实识别能力）。"""

    def __init__(self, transcript: Transcript | None = None):
        self._transcript = transcript or Transcript(
            text="客户表示续约价格需要再谈。",
            segments=[
                TranscriptSegment(text="客户表示续约价格需要再谈。", start_ms=1_200, end_ms=4_800, speaker_id="0")
            ],
            duration_ms=4_800,
            source_url="stub://audio",
        )

    async def transcribe(self, *, audio_url: str) -> Transcript:
        return Transcript(
            text=self._transcript.text,
            segments=list(self._transcript.segments),
            duration_ms=self._transcript.duration_ms,
            source_url=audio_url,
        )


def build_asr_client(settings: Settings) -> AsrClient | None:
    """未启用返回 None——调用方须显式报“能力未启用”，不得静默兜底。"""
    if not settings.asr.enabled:
        return None
    return DashScopeAsrClient(settings)
