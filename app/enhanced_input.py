"""增强输入编排（P5-INPUT 任务 2，需求 12.2/12.3）。

把 file_ref（CRM 附件）变成「已持久化的识别产物」：
受控访问校验 → 复用命中 → 识别（ASR/OCR/文本解析）→ 落库（原文/锚点/供应商任务引用）。

边界：
- 文件标识与文本/锚点持久化在 AI 库；签名 URL 只在内存中即时使用，不落库不落日志；
- 识别与 LLM 整理分阶段：本模块失败即带 stage/code 显式报错，原文在整理失败时仍可用；
- 复用键 = (file_id, source_version, processing, config_version)，文件或配置变化不命中旧结果。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from app.auth import OperatorContext
from app.config import Settings
from app.errors import ValidationFailed
from app.integrations.files import DEFAULT_MAX_BYTES, FileError, parse_bytes
from app.persistence import recognition as recognition_repo
from app.util import redact_error_message

_FILE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
PROCESSINGS = ("asr", "ocr", "parse")


class StageError(Exception):
    """分阶段错误（需求 12.3：失败区分上传、识别、整理、正式写入，不一律重新生成）。

    stage: access（CRM 受控访问）/ config（能力未启用）/ asr / ocr / parse
    """

    def __init__(self, stage: str, code: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message

    def to_payload(self) -> dict[str, str]:
        return {"stage": self.stage, "code": self.code, "message": self.message}


class _CrmFileClient(Protocol):
    async def get_integration_file(self, *, operator: OperatorContext, file_id: str) -> dict[str, Any]: ...

    async def download_integration_file(self, *, operator: OperatorContext, file_id: str) -> bytes: ...


def validate_file_ref(ref: Any) -> tuple[str, int, str]:
    """file_ref 入参校验：{file_id, source_version, processing}。返回规范化三元组。"""
    if not isinstance(ref, dict):
        raise ValidationFailed(
            "input.file_ref 必须是对象 {file_id, source_version, processing}",
            details={"field": "file_ref"},
        )
    file_id = str(ref.get("file_id") or "").strip()
    if not re.fullmatch(_FILE_ID_PATTERN, file_id):
        raise ValidationFailed("file_ref.file_id 非法（限字母数字-_，≤64 位）", details={"field": "file_id"})
    version = ref.get("source_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValidationFailed(
            "file_ref.source_version 必须是正整数（CRM 登记版本）", details={"field": "source_version"}
        )
    processing = str(ref.get("processing") or "").strip()
    if processing not in PROCESSINGS:
        raise ValidationFailed(
            f"file_ref.processing 必须是 {'/'.join(PROCESSINGS)} 之一",
            details={"field": "processing", "received": processing},
        )
    return file_id, version, processing


def recognition_config_version(settings: Settings, processing: str) -> str:
    """识别配置指纹：配置变化（如 ASR 模型/语言、OCR 类型）后不命中旧结果。"""
    if processing == "asr":
        payload = {
            "model": settings.asr.model,
            "language_hints": sorted(settings.asr.language_hints),
            "diarization_enabled": settings.asr.diarization_enabled,
            "workspace_id": settings.asr.workspace_id,
        }
    elif processing == "ocr":
        payload = {"type": settings.ocr.type, "output_coordinate": settings.ocr.output_coordinate}
    else:
        payload = {"parse": "txt-md-csv-json-yaml-v1"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cfg:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _evidence_from_transcript(transcript: Any) -> list[dict[str, Any]]:
    """时间锚点明细；说话人只标号不映射员工。source_url 不落库（URL 不持久化）。"""
    return [asdict(segment) for segment in transcript.segments]


def _evidence_from_ocr(result: Any) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for region in result.regions:
        item = {"text": region.text, "points": [list(point) for point in region.points]}
        evidence.append(item)
    return evidence


async def ensure_recognition(
    pool: AsyncConnectionPool,
    *,
    crm_client: _CrmFileClient,
    operator: OperatorContext,
    file_ref: dict[str, Any],
    settings: Settings,
    asr_client: Any | None = None,
    ocr_client: Any | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """确保 file_ref 有已成功的识别结果；命中复用，未命中执行识别并落库。

    返回 {"reused": bool, "row": 识别产物}。失败抛 StageError 且 running 行落 failed。
    """
    file_id, source_version, processing = validate_file_ref(file_ref)
    config_version = recognition_config_version(settings, processing)

    # Even a cached transcript must pass current CRM ownership/revocation checks.
    access = await crm_client.get_integration_file(operator=operator, file_id=file_id)
    if not access.get("found") or not isinstance(access.get("file"), dict):
        raise StageError("access", "FILE_NOT_FOUND", "附件不存在或无权访问")
    meta = access["file"]
    if meta.get("status") != "ACTIVE" or (access.get("access") or {}).get("type") == "none":
        raise StageError("access", "FILE_REVOKED", "附件已撤权，不能再处理")
    if int(meta.get("source_version") or 0) != source_version:
        raise StageError(
            "access",
            "FILE_VERSION_CHANGED",
            f"来源版本已变化（登记 {meta.get('source_version')} ≠ 引用 {source_version}），必须显式重新处理",
        )

    if processing == "asr" and not settings.asr.enabled:
        raise StageError("config", "ASR_DISABLED", "asr.enabled=false，录音识别未启用")
    if processing == "ocr" and not settings.ocr.enabled:
        raise StageError("config", "OCR_DISABLED", "ocr.enabled=false，图片识别未启用")

    reusable = await recognition_repo.get_reusable(
        pool, file_id=file_id, source_version=source_version, processing=processing, config_version=config_version
    )
    if reusable is not None:
        return {"reused": True, "row": reusable}

    provider = {"asr": "dashscope-asr", "ocr": "aliyun-ocr", "parse": "builtin-parse"}[processing]
    row = (
        await recognition_repo.get_resumable_task(
            pool, file_id=file_id, source_version=source_version,
            processing=processing, config_version=config_version,
        ) if processing == "asr" else None
    )
    if row is None:
        row = await recognition_repo.create(
            pool, run_id=run_id, file_id=file_id, source_version=source_version,
            processing=processing, provider=provider, config_version=config_version,
        )
    recognition_id = row["recognition_id"]
    try:
        raw_text, anchored_text, evidence, duration_ms, provider, provider_task_id = await _recognize(
            access=access,
            file_meta=meta,
            processing=processing,
            settings=settings,
            crm_client=crm_client,
            operator=operator,
            asr_client=asr_client,
            ocr_client=ocr_client,
            pool=pool,
            recognition_id=recognition_id,
            provider_task_id=str(row.get("provider_task_id") or ""),
        )
    except StageError as exc:
        if exc.code != "ASR_POLL_RETRY":
            await recognition_repo.fail(pool, recognition_id, error=exc.to_payload())
        raise
    except Exception as exc:
        error = StageError(processing, "RECOGNITION_FAILED", "识别服务失败；可重试，文件权限会重新校验")
        await recognition_repo.fail(pool, recognition_id, error=error.to_payload())
        raise error from exc
    row = await recognition_repo.complete(
        pool,
        recognition_id,
        raw_text=raw_text,
        anchored_text=anchored_text,
        evidence=evidence,
        duration_ms=duration_ms,
        provider_task_id=provider_task_id,
    )
    return {"reused": False, "row": row}


async def _recognize(
    *,
    access: dict[str, Any],
    file_meta: dict[str, Any],
    processing: str,
    settings: Settings,
    crm_client: _CrmFileClient,
    operator: OperatorContext,
    asr_client: Any | None,
    ocr_client: Any | None,
    pool: AsyncConnectionPool,
    recognition_id: str,
    provider_task_id: str,
) -> tuple[str, str, list[dict[str, Any]], int | None, str, str]:
    """按处理类型执行识别，返回 (原文, 锚点文本, 锚点明细, 时长, provider, provider_task_id)。"""
    access_type = (access.get("access") or {}).get("type")
    url = (access.get("access") or {}).get("url")
    file_id = str(file_meta["id"])

    if processing == "parse":
        data = await _download_inline(crm_client, operator, file_id, access_type)
        name = str(file_meta.get("file_name") or "")
        try:
            parsed = parse_bytes(
                data=data, name=name, content_type=str(file_meta.get("content_type") or ""), max_bytes=DEFAULT_MAX_BYTES
            )
        except FileError as exc:
            # 解析异常会落库（识别行 + Run 错误），脱敏后再作为阶段错误抛出
            raise StageError("parse", "PARSE_UNSUPPORTED", redact_error_message(str(exc), limit=120)) from exc
        evidence = [{"truncated": parsed.truncated, "encoding": parsed.encoding, "file_type": parsed.file_type}]
        return parsed.text, "", evidence, None, "builtin-parse", ""

    if processing == "ocr":
        if ocr_client is None:
            from app.integrations.ocr import build_ocr_client

            ocr_client = build_ocr_client(settings)
        if ocr_client is None:
            raise StageError("config", "OCR_DISABLED", "ocr.enabled=false，图片识别未启用")
        if access_type == "signed_url" and url:
            result = await ocr_client.recognize(image_url=url)
        else:
            data = await _download_inline(crm_client, operator, file_id, access_type)
            result = await ocr_client.recognize(image_bytes=data)
        return (
            result.text,
            result.anchored_text(),
            _evidence_from_ocr(result),
            None,
            "aliyun-ocr",
            "",
        )

    # asr：供应商只接受公网 URL；CRM local 存储模式无 URL，显式失败不降级
    if not (access_type == "signed_url" and url):
        raise StageError("asr", "ASR_REQUIRES_PUBLIC_URL", "ASR 只接受公网可访问 URL；CRM 存储为 local 模式，无法提供")
    owns_asr_client = asr_client is None
    if asr_client is None:
        from app.integrations.asr import build_asr_client

        asr_client = build_asr_client(settings)
    if asr_client is None:
        raise StageError("config", "ASR_DISABLED", "asr.enabled=false，录音识别未启用")
    try:
        if hasattr(asr_client, "submit") and hasattr(asr_client, "poll"):
            task_id = provider_task_id or await asr_client.submit(audio_url=url)
            if not provider_task_id:
                await recognition_repo.set_provider_task(pool, recognition_id, task_id)
            try:
                transcript = await asr_client.poll(task_id=task_id, source_url=url)
            except Exception as exc:
                raise StageError("asr", "ASR_POLL_RETRY", "识别任务未取得结果，可用已保存的 task_id 重试") from exc
        else:
            transcript = await asr_client.transcribe(audio_url=url)
            task_id = ""
        return (
            transcript.text,
            transcript.anchored_text(),
            _evidence_from_transcript(transcript),
            transcript.duration_ms or None,
            "dashscope-asr",
            task_id,
        )
    finally:
        if owns_asr_client and hasattr(asr_client, "aclose"):
            await asr_client.aclose()


async def _download_inline(
    crm_client: _CrmFileClient, operator: OperatorContext, file_id: str, access_type: str | None
) -> bytes:
    if access_type != "inline":
        raise StageError("access", "FILE_INLINE_UNAVAILABLE", "该附件未提供 inline 字节访问（oss 模式请用签名 URL）")
    data = await crm_client.download_integration_file(operator=operator, file_id=file_id)
    if not data:
        raise StageError("access", "FILE_EMPTY", "附件内容为空")
    return data
