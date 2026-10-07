"""Run file_ref entry and separated ASR submission behavior."""
from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.api.routes.runs import CreateRunBody, _validate_capability_input
from app.capabilities import get_capability
from app.config import Settings
from app.enhanced_input import StageError, ensure_recognition
from app.auth import OperatorContext
from app.persistence import recognition as recognition_repo
from app.integrations.asr import DashScopeAsrClient
from app.integrations.organize import organize_text
from app.runtime.executor import Executor
from app.persistence import runs as runs_repo
from app.providers.stub import StubChatModel


def test_file_ref_is_accepted_instead_of_text_only_for_extract():
    extract = get_capability("communication.extract")
    assert extract is not None
    body = CreateRunBody(capability=extract.name, input={
        "file_ref": {"file_id": "RS0000000001", "source_version": 1, "processing": "parse"}
    })
    _validate_capability_input(extract, body)
    with pytest.raises(Exception, match="二选一"):
        _validate_capability_input(extract, CreateRunBody(capability=extract.name, input={
            **body.input, "text": "duplicate"
        }))


class Model:
    async def ainvoke(self, prompt):
        return AIMessage(content=json.dumps({"optimized_text": "客户 2026-10-07 续约 50 万。"}))


async def test_organizer_keeps_original_numbers_and_rejects_changed_numbers():
    assert await organize_text(Model(), "客户 2026-10-07 续约 50 万") == "客户 2026-10-07 续约 50 万。"

    class WrongModel:
        async def ainvoke(self, prompt):
            return AIMessage(content='{"optimized_text":"客户 2026-10-08 续约 50 万"}')

    with pytest.raises(StageError, match="数字或日期"):
        await organize_text(WrongModel(), "客户 2026-10-07 续约 50 万")


async def test_executor_exposes_three_layers_without_autoconfirm(monkeypatch):
    async def fake_recognition(*args, **kwargs):
        return {"row": {
            "file_id": "RS0000000001", "source_version": 1, "processing": "parse",
            "recognition_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "raw_text": "客户 2026-10-07 续约 50 万", "anchored_text": "", "evidence": [],
        }}

    monkeypatch.setattr("app.runtime.executor.ensure_recognition", fake_recognition)
    executor = Executor(None, {}, lease_seconds=30, settings=Settings(), crm_client=object(), model=Model())
    text, layers = await executor._prepare_file_input(
        {"run_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "operator": {"user_id": "U1", "user_name": "tester"}},
        {"file_ref": {"file_id": "RS0000000001", "source_version": 1, "processing": "parse"}},
    )
    assert text == layers["optimized_text"]
    assert layers["raw_text"] != layers["optimized_text"]
    assert layers["final_text"] is None
    assert layers["requires_human_confirmation"] is True


async def test_asr_submit_and_poll_can_resume_by_task_id():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/transcription"):
            return httpx.Response(200, json={"output": {"task_id": "existing-1"}})
        if request.url.path.endswith("/existing-1"):
            return httpx.Response(200, json={"output": {
                "task_status": "SUCCEEDED",
                "results": [{"transcription_url": "https://result.oss-cn-beijing.aliyuncs.com/x.json"}],
            }})
        return httpx.Response(200, json={"transcripts": [{"text": "识别原文", "sentences": []}]})

    settings = Settings(asr={"enabled": True, "api_key": "test"})
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DashScopeAsrClient(settings, http_client=http_client)
    task_id = await client.submit(audio_url="https://audio.example/a.wav")
    assert task_id == "existing-1"
    resumed = await client.poll(task_id=task_id, source_url="https://new-signed-url.example/a.wav")
    assert resumed.text == "识别原文"
    assert sum(path.endswith("/transcription") for path in seen) == 1
    await http_client.aclose()


@pytest.mark.realpg
async def test_file_ref_runs_through_graph_and_preserves_layers(db_pool, graphs, test_settings):
    class Crm:
        async def get_integration_file(self, *, operator, file_id):
            return {"found": True, "file": {
                "id": file_id, "source_version": 1, "status": "ACTIVE",
                "file_name": "note.txt", "content_type": "text/plain",
            }, "access": {"type": "inline"}}

        async def download_integration_file(self, *, operator, file_id):
            return "客户：ACME\n任务：回访\n2026-10-07\n负责人：张三".encode()

    run, _ = await runs_repo.create_run(
        db_pool, idempotency_key="file-ref-graph-test", capability="communication.extract",
        input_payload={"file_ref": {"file_id": "RS0000000001", "source_version": 1, "processing": "parse"}},
        operator={"user_id": "ST0000000001", "user_name": "tester"}, thread_id=None,
        conversation_id=None, graph_version="extract@1", prompt_version="extract-prompt@1",
        max_attempts=1,
    )
    executor = Executor(db_pool, graphs, lease_seconds=60, settings=test_settings,
                        crm_client=Crm(), model=StubChatModel())
    assert await executor.run_batch() == 1
    finished = await runs_repo.get_run(db_pool, run["run_id"])
    assert finished["status"] == "succeeded", finished.get("error")
    layers = finished["result"]["enhanced_input"]
    assert layers["raw_text"].startswith("客户：ACME")
    assert layers["optimized_text"] == layers["raw_text"]
    assert layers["final_text"] is None
    assert finished["result"]["candidates"]["tasks"][0]["due_date"] == "2026-10-07"


@pytest.mark.realpg
async def test_asr_retry_polls_saved_task_without_resubmitting(db_pool):
    class Crm:
        async def get_integration_file(self, *, operator, file_id):
            return {"found": True, "file": {"id": file_id, "source_version": 1, "status": "ACTIVE"},
                    "access": {"type": "signed_url", "url": "https://bucket.aliyuncs.com/a?sig=fresh"}}

    class Asr:
        submits = 0
        polls = 0

        async def submit(self, *, audio_url):
            self.submits += 1
            return "provider-task-1"

        async def poll(self, *, task_id, source_url):
            self.polls += 1
            assert task_id == "provider-task-1"
            if self.polls == 1:
                raise TimeoutError("temporary")
            from app.integrations.asr import Transcript
            return Transcript(text="原文", source_url=source_url)

    asr = Asr()
    settings = Settings(asr={"enabled": True, "api_key": "test"})
    ref = {"file_id": "RS0000000002", "source_version": 1, "processing": "asr"}
    operator = OperatorContext(user_id="U1", user_name="tester")
    with pytest.raises(StageError) as exc:
        await ensure_recognition(db_pool, crm_client=Crm(), operator=operator,
                                 file_ref=ref, settings=settings, asr_client=asr)
    assert exc.value.code == "ASR_POLL_RETRY"
    history = await recognition_repo.list_by_file(db_pool, file_id=ref["file_id"])
    assert history[0]["provider_task_id"] == "provider-task-1"
    assert history[0]["status"] == "running"
    result = await ensure_recognition(db_pool, crm_client=Crm(), operator=operator,
                                      file_ref=ref, settings=settings, asr_client=asr)
    assert result["row"]["status"] == "succeeded"
    assert asr.submits == 1 and asr.polls == 2


@pytest.mark.realpg
async def test_cached_recognition_is_denied_after_crm_revocation(db_pool):
    class Crm:
        revoked = False

        async def get_integration_file(self, *, operator, file_id):
            return {"found": True, "file": {
                "id": file_id, "source_version": 1,
                "status": "REVOKED" if self.revoked else "ACTIVE",
                "file_name": "x.txt", "content_type": "text/plain",
            }, "access": {"type": "none" if self.revoked else "inline"}}

        async def download_integration_file(self, *, operator, file_id):
            return b"original text"

    crm = Crm()
    ref = {"file_id": "RS0000000003", "source_version": 1, "processing": "parse"}
    operator = OperatorContext(user_id="U1", user_name="tester")
    settings = Settings()
    await ensure_recognition(db_pool, crm_client=crm, operator=operator, file_ref=ref, settings=settings)
    crm.revoked = True
    with pytest.raises(StageError) as exc:
        await ensure_recognition(db_pool, crm_client=crm, operator=operator, file_ref=ref, settings=settings)
    assert exc.value.code == "FILE_REVOKED"


@pytest.mark.realpg
async def test_cancelled_recognition_cannot_be_completed_late(db_pool):
    row = await recognition_repo.create(
        db_pool, run_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        file_id="RS0000000004", source_version=1, processing="ocr", config_version="cfg:test",
    )
    assert await recognition_repo.cancel_by_run(db_pool, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb") == 1
    with pytest.raises(recognition_repo.RecognitionRepoError, match="迟到结果"):
        await recognition_repo.complete(db_pool, row["recognition_id"], raw_text="late")
    stored = await recognition_repo.get_by_id(db_pool, row["recognition_id"])
    assert stored["status"] == "cancelled"
