"""P5-INPUT 任务 2：识别产物持久化与增强输入编排测试（真实 PG + Stub 适配器，不联网）。

覆盖（需求 12.2/12.3）：
- 识别产物表往返：running → succeeded/failed/cancelled 状态流转；
- 复用语义：同 (file_id, source_version, processing, config_version) 成功结果只存一条，
  重复 ensure_recognition 不再调用供应商；版本或配置变化不命中旧结果；
- 分阶段错误：access（不存在/撤权/版本变化/未启用）→ StageError 且 running 行落 failed；
- 锚点持久化：ASR 时间锚点/说话人标号、OCR 区域坐标进 evidence；URL 不落库；
- ASR 在 CRM local 存储模式（无公网 URL）显式失败，不降级假成功。
"""
from __future__ import annotations

import pytest

from app.auth import OperatorContext
from app.config import Settings
from app.enhanced_input import StageError, ensure_recognition, recognition_config_version, validate_file_ref
from app.errors import ValidationFailed
from app.integrations.asr import Transcript, TranscriptSegment
from app.integrations.ocr import OcrRegion, OcrResult
from app.persistence import recognition as repo

pytestmark = pytest.mark.realpg

OPERATOR = OperatorContext(user_id="ST0000000001", user_name="测试销售")
FILE_REF = {"file_id": "RS0000000001", "source_version": 1, "processing": "parse"}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "test",
        "database_url": "postgresql://postgres@127.0.0.1:1/x",
        "model": {"provider": "stub"},
        "auth": {"service_secret": "test-secret"},
    }
    base.update(overrides)
    return Settings(**base)


class FakeCrmFileClient:
    """模拟 CRM 受控访问端点行为（元数据 + inline 字节）。"""

    def __init__(self, files: dict[str, dict], payloads: dict[str, bytes] | None = None):
        self._files = files
        self._payloads = payloads or {}
        self.meta_calls = 0
        self.download_calls = 0

    async def get_integration_file(self, *, operator: OperatorContext, file_id: str) -> dict:
        self.meta_calls += 1
        meta = self._files.get(file_id)
        if meta is None:
            return {"found": False, "file": None}
        return {"found": True, "file": meta, "access": meta["_access"]}

    async def download_integration_file(self, *, operator: OperatorContext, file_id: str) -> bytes:
        self.download_calls += 1
        return self._payloads.get(file_id, b"")


class FakeAsrClient:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, *, audio_url: str):
        self.calls += 1
        return Transcript(
            text="客户表示续约价格需要再谈。",
            segments=[TranscriptSegment(text="客户表示续约价格需要再谈。", start_ms=1200, end_ms=4800, speaker_id="0")],
            duration_ms=4800,
            source_url=audio_url,
        )


class FakeOcrClient:
    def __init__(self):
        self.calls = 0
        self.last_kwargs: dict = {}

    async def recognize(self, *, image_url: str | None = None, image_bytes: bytes | None = None):
        self.calls += 1
        self.last_kwargs = {"url": image_url, "bytes": image_bytes}
        return OcrResult(
            text="满 50 万减 2 万",
            regions=(OcrRegion(text="满 50 万减 2 万", points=((10, 20), (300, 20), (300, 60), (10, 60))),),
        )


def _file_meta(
    file_id: str = "RS0000000001",
    *,
    status: str = "ACTIVE",
    file_name: str = "拜访纪要.txt",
    access: dict | None = None,
) -> dict:
    return {
        "id": file_id,
        "file_name": file_name,
        "content_type": "text/plain",
        "file_kind": "document",
        "size_bytes": 64,
        "source_version": 1,
        "status": status,
        "_access": access or {"type": "inline", "content_path": f"/api/v1/salescrm/ai/integration/files/{file_id}/content"},
    }


# ---------------------------------------------------------------- file_ref 校验


def test_validate_file_ref_normalizes_and_rejects():
    assert validate_file_ref({"file_id": "RS0000000001", "source_version": 1, "processing": "ocr"}) == (
        "RS0000000001",
        1,
        "ocr",
    )
    for bad in (
        {"file_id": "bad/id", "source_version": 1, "processing": "parse"},
        {"file_id": "RS0000000001", "source_version": 0, "processing": "parse"},
        {"file_id": "RS0000000001", "source_version": True, "processing": "parse"},
        {"file_id": "RS0000000001", "source_version": 1, "processing": "magic"},
        {"file_id": "RS0000000001", "source_version": 1},
        "not-a-dict",
    ):
        with pytest.raises(ValidationFailed):
            validate_file_ref(bad)


def test_config_version_tracks_relevant_settings():
    parse_a = recognition_config_version(_settings(), "parse")
    parse_b = recognition_config_version(
        _settings(asr={"enabled": True, "api_key": "k", "model": "paraformer-v2"}), "parse"
    )
    assert parse_a == parse_b  # 解析配置与 ASR 设置无关

    asr_a = recognition_config_version(_settings(), "asr")
    asr_b = recognition_config_version(
        _settings(asr={"enabled": True, "api_key": "k", "model": "paraformer-v2"}), "asr"
    )
    asr_c = recognition_config_version(
        _settings(asr={"enabled": True, "api_key": "k", "model": "paraformer-v8k"}), "asr"
    )
    assert asr_a == asr_b  # enabled 是门禁不是识别参数：指纹只含影响结果的字段
    assert asr_b != asr_c  # 模型变化 → 新配置版本（不命中旧结果）

    ocr_a = recognition_config_version(_settings(), "ocr")
    ocr_b = recognition_config_version(
        _settings(ocr={"enabled": True, "access_key_id": "a", "access_key_secret": "b"}), "ocr"
    )
    ocr_c = recognition_config_version(
        _settings(ocr={"enabled": True, "access_key_id": "a", "access_key_secret": "b", "type": "General"}), "ocr"
    )
    assert ocr_a == ocr_b  # 凭据不是识别参数，不影响指纹
    assert ocr_b != ocr_c  # OCR 类型变化 → 新配置版本


# ---------------------------------------------------------------- repo 状态流转


async def test_repo_roundtrip_and_reuse_key(db_pool):
    row = await repo.create(
        db_pool,
        run_id=None,
        file_id="RS0000000001",
        source_version=1,
        processing="parse",
        provider="builtin-parse",
        config_version="cfg:a",
    )
    assert row["status"] == "running"

    assert await repo.get_reusable(db_pool, file_id="RS0000000001", source_version=1, processing="parse", config_version="cfg:a") is None

    await repo.set_provider_task(db_pool, row["recognition_id"], "task-1")

    done = await repo.complete(
        db_pool,
        row["recognition_id"],
        raw_text="正文",
        anchored_text="[00:00:01] 正文",
        evidence=[{"truncated": False}],
        duration_ms=None,
    )
    assert done["status"] == "succeeded"
    assert done["raw_text"] == "正文"
    assert done["evidence"] == [{"truncated": False}]

    reusable = await repo.get_reusable(db_pool, file_id="RS0000000001", source_version=1, processing="parse", config_version="cfg:a")
    assert reusable is not None and reusable["recognition_id"] == row["recognition_id"]

    # 版本变化不命中
    assert await repo.get_reusable(db_pool, file_id="RS0000000001", source_version=2, processing="parse", config_version="cfg:a") is None
    # 配置变化不命中
    assert await repo.get_reusable(db_pool, file_id="RS0000000001", source_version=1, processing="parse", config_version="cfg:b") is None

    again = await repo.get_by_id(db_pool, row["recognition_id"])
    assert again["provider_task_id"] == "task-1"

    history = await repo.list_by_file(db_pool, file_id="RS0000000001")
    assert [r["recognition_id"] for r in history] == [row["recognition_id"]]


async def test_repo_fail_and_cancel_transitions(db_pool):
    failed = await repo.create(db_pool, run_id=None, file_id="RS0000000002", source_version=1, processing="asr", config_version="cfg:a")
    await repo.fail(db_pool, failed["recognition_id"], error={"stage": "asr", "code": "X", "message": "失败"})
    row = await repo.get_by_id(db_pool, failed["recognition_id"])
    assert row["status"] == "failed" and row["error"]["stage"] == "asr"
    assert await repo.get_reusable(db_pool, file_id="RS0000000002", source_version=1, processing="asr", config_version="cfg:a") is None

    cancelled = await repo.create(db_pool, run_id=None, file_id="RS0000000003", source_version=1, processing="ocr", config_version="cfg:a")
    assert await repo.cancel(db_pool, cancelled["recognition_id"]) is True
    assert await repo.cancel(db_pool, cancelled["recognition_id"]) is False  # 幂等：非 running 不能再取消


# ---------------------------------------------------------------- 编排：复用与分阶段错误


async def test_ensure_recognition_parse_reuses_without_second_recognition(db_pool):
    crm = FakeCrmFileClient(
        files={"RS0000000001": _file_meta()},
        payloads={"RS0000000001": "10月7日拜访恒通物流，商谈续约。".encode("utf-8")},
    )
    settings = _settings()

    first = await ensure_recognition(
        db_pool, crm_client=crm, operator=OPERATOR, file_ref=FILE_REF, settings=settings
    )
    assert first["reused"] is False
    assert "续约" in first["row"]["raw_text"]
    assert first["row"]["provider"] == "builtin-parse"
    assert first["row"]["error"] is None

    second = await ensure_recognition(
        db_pool, crm_client=crm, operator=OPERATOR, file_ref=FILE_REF, settings=settings
    )
    assert second["reused"] is True
    assert second["row"]["recognition_id"] == first["row"]["recognition_id"]
    assert crm.download_calls == 1  # 识别结果复用，不再重复取文件


async def test_ensure_recognition_stage_errors_persist_failure(db_pool):
    settings = _settings()

    missing = FakeCrmFileClient(files={})
    with pytest.raises(StageError) as exc:
        await ensure_recognition(
            db_pool, crm_client=missing, operator=OPERATOR,
            file_ref={"file_id": "RS0000000009", "source_version": 1, "processing": "parse"}, settings=settings,
        )
    assert (exc.value.stage, exc.value.code) == ("access", "FILE_NOT_FOUND")

    revoked_meta = _file_meta(status="REVOKED", access={"type": "none"})
    revoked = FakeCrmFileClient(files={"RS0000000001": revoked_meta})
    with pytest.raises(StageError) as exc:
        await ensure_recognition(db_pool, crm_client=revoked, operator=OPERATOR, file_ref=FILE_REF, settings=settings)
    assert exc.value.code == "FILE_REVOKED"

    stale = FakeCrmFileClient(files={"RS0000000001": dict(_file_meta(), source_version=2)})
    with pytest.raises(StageError) as exc:
        await ensure_recognition(
            db_pool, crm_client=stale, operator=OPERATOR,
            file_ref={"file_id": "RS0000000001", "source_version": 1, "processing": "parse"}, settings=settings,
        )
    assert exc.value.code == "FILE_VERSION_CHANGED"

    # disabled：asr/ocr 未启用 → config 阶段错误，且不产生 running 行
    disabled = FakeCrmFileClient(files={"RS0000000001": _file_meta()})
    with pytest.raises(StageError) as exc:
        await ensure_recognition(
            db_pool, crm_client=disabled, operator=OPERATOR,
            file_ref={"file_id": "RS0000000001", "source_version": 1, "processing": "asr"}, settings=settings,
        )
    assert (exc.value.stage, exc.value.code) == ("config", "ASR_DISABLED")

    history = await repo.list_by_file(db_pool, file_id="RS0000000009")
    assert len(history) == 0  # access 阶段失败不建行


async def test_ensure_recognition_ocr_persists_anchors_and_blocks_url_leak(db_pool):
    meta = _file_meta(access={"type": "signed_url", "url": "https://bucket.example/signed?sig=SECRET"})
    crm = FakeCrmFileClient(files={"RS0000000001": meta})
    ocr = FakeOcrClient()
    settings = _settings(ocr={"enabled": True, "access_key_id": "a", "access_key_secret": "b"})

    result = await ensure_recognition(
        db_pool, crm_client=crm, operator=OPERATOR,
        file_ref={"file_id": "RS0000000001", "source_version": 1, "processing": "ocr"},
        settings=settings, ocr_client=ocr,
    )
    row = result["row"]
    assert ocr.last_kwargs["url"] == meta["_access"]["url"]  # oss 模式走签名 URL
    assert "<R1@10,20,300,20,300,60,10,60>" in row["anchored_text"]
    assert row["evidence"][0]["points"][0] == [10, 20]
    assert "SECRET" not in (row["raw_text"] + row["anchored_text"] + str(row["evidence"]))  # URL/签名不落库


async def test_ensure_recognition_asr_requires_public_url(db_pool):
    crm = FakeCrmFileClient(files={"RS0000000001": _file_meta()})  # local 模式 → inline
    settings = _settings(asr={"enabled": True, "api_key": "k"})

    with pytest.raises(StageError) as exc:
        await ensure_recognition(
            db_pool, crm_client=crm, operator=OPERATOR,
            file_ref={"file_id": "RS0000000001", "source_version": 1, "processing": "asr"},
            settings=settings, asr_client=FakeAsrClient(),
        )
    assert exc.value.code == "ASR_REQUIRES_PUBLIC_URL"
    row = (await repo.list_by_file(db_pool, file_id="RS0000000001"))[0]
    assert row["status"] == "failed" and row["error"]["stage"] == "asr"


async def test_ensure_recognition_asr_persists_time_anchors(db_pool):
    meta = _file_meta(
        file_name="拜访录音.m4a",
        access={"type": "signed_url", "url": "https://bucket.example/audio.m4a?sig=1"},
    )
    crm = FakeCrmFileClient(files={"RS0000000001": meta})
    asr = FakeAsrClient()
    settings = _settings(asr={"enabled": True, "api_key": "k"})

    result = await ensure_recognition(
        db_pool, crm_client=crm, operator=OPERATOR,
        file_ref={"file_id": "RS0000000001", "source_version": 1, "processing": "asr"},
        settings=settings, asr_client=asr,
    )
    row = result["row"]
    assert asr.calls == 1
    assert row["duration_ms"] == 4800
    assert "[00:00:01]" in row["anchored_text"] and "说话人0" in row["anchored_text"]
    assert row["evidence"][0]["speaker_id"] == "0"  # 说话人只标号不映射员工
    assert row["provider"] == "dashscope-asr"

    # 同键第二次：直接复用，不重复调用供应商
    again = await ensure_recognition(
        db_pool, crm_client=crm, operator=OPERATOR,
        file_ref={"file_id": "RS0000000001", "source_version": 1, "processing": "asr"},
        settings=settings, asr_client=asr,
    )
    assert again["reused"] is True and asr.calls == 1
