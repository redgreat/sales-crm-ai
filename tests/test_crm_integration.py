"""P4 CRM 集成端到端测试：真实文字 → AI 抽取 → CRM 候选导入 → 确认写入。

测试分两层：
1. AI 侧（内置 API，无需外部认证）：Run 创建 → 抽取 → 缺参 → 恢复 → 候选
2. CRM 侧（需显式 SAI_RUN_CRM_LIVE=1 和有效 JWT）：导入候选 → 确认写入正式对象

用法:
  # 仅测 AI 侧（默认，无需 token）
  pytest tests/test_crm_integration.py -v

  # 全流程（含 CRM 写入）
  $env:SAI_RUN_CRM_LIVE = "1"
  $env:SAI_CRM_TOKEN = "Bearer eyJ..."
  $env:SAI_CRM_BASE_URL = "http://127.0.0.1:8080"   # 可选，默认 8080
  pytest tests/test_crm_integration.py -v -m crmlive
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.auth import OperatorContext, sign_request
from app.config import Settings

pytestmark = pytest.mark.realpg

# ============ 环境变量 ============
CRM_BASE_URL = os.environ.get("SAI_CRM_BASE_URL", "http://127.0.0.1:8080")
AI_BASE_URL = os.environ.get("SAI_AI_BASE_URL", "http://127.0.0.1:8310")


def _load_crm_token() -> str:
    """优先环境变量，其次 .local/crm-token.txt 文件。"""
    token = os.environ.get("SAI_CRM_TOKEN", "")
    if token:
        return token
    token_file = Path(__file__).resolve().parent.parent / ".local" / "crm-token.txt"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


CRM_LIVE_ENABLED = os.environ.get("SAI_RUN_CRM_LIVE") == "1"
CRM_TOKEN = _load_crm_token() if CRM_LIVE_ENABLED else ""

# CRM 写入测试需要 token
requires_crm = pytest.mark.skipif(
    not CRM_LIVE_ENABLED or not CRM_TOKEN,
    reason="真实 CRM 写入测试需显式 SAI_RUN_CRM_LIVE=1 和有效 JWT",
)


# ============ fixtures ============
def _dev_settings(db_url: str) -> Settings:
    return Settings(
        environment="dev",
        database_url=db_url,
        model={"provider": "stub"},
        auth={"service_secret": "dev-secret", "service_key_id": "crm-ai"},
        worker={
            "enabled": True,
            "concurrency": 2,
            "poll_interval_seconds": 0.05,
            "lease_seconds": 120,
            "backoff_base_seconds": 0.2,
        },
    )


@pytest_asyncio.fixture
async def ai_api(db_url: str):
    """内置 AI API（ASGI transport，Stub 模型）。"""
    from app.api.main import create_app

    settings = _dev_settings(db_url)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai") as client:
            yield client, settings


@pytest_asyncio.fixture
async def ai_live():
    """直连运行中的 AI 服务（playground 代理，用于真实模型测试）。"""
    async with httpx.AsyncClient(base_url=AI_BASE_URL, timeout=60) as client:
        yield client


@pytest_asyncio.fixture
async def crm_api():
    """CRM Java API 客户端（需 JWT）。"""
    if not CRM_TOKEN:
        pytest.skip("需要 SAI_CRM_TOKEN")
    headers = {"Authorization": CRM_TOKEN, "Content-Type": "application/json"}
    async with httpx.AsyncClient(base_url=CRM_BASE_URL, headers=headers, timeout=30) as client:
        yield client


# ============ helpers ============
def _signed(settings: Settings, method: str, path: str, body: bytes, user_id: str = "e2e-user") -> dict[str, str]:
    return sign_request(
        secret=settings.auth.service_secret,
        key_id=settings.auth.service_key_id,
        method=method,
        path=path,
        body=body,
        operator=OperatorContext(user_id=user_id, user_name="E2E测试"),
    )


async def _post_signed(client, settings, path, payload, user_id="e2e-user"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _signed(settings, "POST", path, body, user_id)
    headers["Content-Type"] = "application/json"
    return await client.post(path, content=body, headers=headers)


async def _get_signed(client, settings, path, user_id="e2e-user"):
    return await client.get(path, headers=_signed(settings, "GET", path, b"", user_id))


async def _wait_status(client, settings, run_id, statuses, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await _get_signed(client, settings, f"/api/v1/runs/{run_id}")
        body = r.json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.15)
    raise TimeoutError(f"Run {run_id} 在 {timeout}s 内未达到 {statuses}")


# ============ AI 侧测试（无需外部认证） ============
class TestAiSideFlow:
    """AI 侧 P4 流程：Run 创建 → 抽取 → 缺参 → 恢复 → 候选产出。"""

    async def test_create_run_and_extract(self, ai_api):
        """创建 Run → Stub 抽取 → 直接产出候选（Stub 不缺参）。"""
        client, settings = ai_api
        payload = {
            "capability": "communication.extract",
            "input": {"text": "今天拜访了客户A公司，讨论了系统采购。"},
            "idempotency_key": f"p4-{uuid.uuid4().hex[:12]}",
        }
        r = await _post_signed(client, settings, "/api/v1/runs", payload)
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        result = await _wait_status(client, settings, run_id, {"succeeded", "failed", "waiting_input"})
        assert result["status"] == "succeeded"
        assert result["result"] is not None
        assert "candidates" in result["result"]

    async def test_idempotent_create_returns_same_run(self, ai_api):
        """相同幂等键创建 Run 返回同一条，不重复创建。"""
        client, settings = ai_api
        key = f"idem-{uuid.uuid4().hex[:12]}"
        payload = {
            "capability": "communication.extract",
            "input": {"text": "测试幂等"},
            "idempotency_key": key,
        }
        r1 = await _post_signed(client, settings, "/api/v1/runs", payload)
        r2 = await _post_signed(client, settings, "/api/v1/runs", payload)
        assert r1.json()["run_id"] == r2.json()["run_id"]
        assert r2.json()["idempotent_replay"] is True

    async def test_missing_params_and_resume(self, ai_api):
        """缺参追问 → 补参恢复 → 候选产出（使用带缺失字段的输入）。"""
        client, settings = ai_api
        # 用一个会触发缺参的文本（Stub 模型对特定 pattern 返回缺参）
        payload = {
            "capability": "communication.extract",
            "input": {"text": "需要准备方案报价，负责人王明。客户：腾讯科技。"},
            "idempotency_key": f"miss-{uuid.uuid4().hex[:12]}",
        }
        r = await _post_signed(client, settings, "/api/v1/runs", payload)
        run_id = r.json()["run_id"]

        result = await _wait_status(client, settings, run_id, {"succeeded", "waiting_input", "failed"})
        if result["status"] == "waiting_input":
            # 获取缺参问题
            pending = await _get_signed(client, settings, f"/api/v1/runs/{run_id}/pending")
            assert pending.status_code == 200
            body = pending.json()
            assert body["waiting"] is True

            # 补参恢复
            resume_payload = {
                "state_version": body["state_version"],
                "values": {"due_date": "2026-10-15"},
            }
            rr = await _post_signed(client, settings, f"/api/v1/runs/{run_id}/resume", resume_payload)
            assert rr.status_code == 202

            final = await _wait_status(client, settings, run_id, {"succeeded", "failed"})
            assert final["status"] == "succeeded"
            assert final["result"] is not None

    async def test_cross_user_denied(self, ai_api):
        """跨用户访问他人 Run 拒绝。"""
        client, settings = ai_api
        payload = {
            "capability": "communication.extract",
            "input": {"text": "测试跨用户"},
            "idempotency_key": f"cross-{uuid.uuid4().hex[:12]}",
        }
        r = await _post_signed(client, settings, "/api/v1/runs", payload, user_id="user-A")
        run_id = r.json()["run_id"]

        # 另一用户访问
        r2 = await _get_signed(client, settings, f"/api/v1/runs/{run_id}", user_id="user-B")
        assert r2.status_code in (403, 404)

    async def test_cancel_run(self, ai_api):
        """取消 Run → 状态变为 cancelled。"""
        client, settings = ai_api
        payload = {
            "capability": "communication.extract",
            "input": {"text": "测试取消"},
            "idempotency_key": f"cancel-{uuid.uuid4().hex[:12]}",
        }
        r = await _post_signed(client, settings, "/api/v1/runs", payload)
        run_id = r.json()["run_id"]

        cr = await _post_signed(client, settings, f"/api/v1/runs/{run_id}/cancel", {})
        assert cr.status_code in (200, 202)

        result = await _wait_status(client, settings, run_id, {"cancelled", "succeeded", "failed"})
        assert result["status"] in ("cancelled", "succeeded")  # 快速完成的可能来不及取消

    async def test_replay_resume_rejected(self, ai_api):
        """重复 resume 拒绝（RUN_NOT_RESUMABLE）。"""
        client, settings = ai_api
        payload = {
            "capability": "communication.extract",
            "input": {"text": "测试重复恢复"},
            "idempotency_key": f"replay-{uuid.uuid4().hex[:12]}",
        }
        r = await _post_signed(client, settings, "/api/v1/runs", payload)
        run_id = r.json()["run_id"]
        result = await _wait_status(client, settings, run_id, {"succeeded", "waiting_input", "failed"})

        if result["status"] == "waiting_input":
            pending = await _get_signed(client, settings, f"/api/v1/runs/{run_id}/pending")
            body = pending.json()
            resume_payload = {"state_version": body["state_version"], "values": {"due_date": "2026-10-01"}}
            r1 = await _post_signed(client, settings, f"/api/v1/runs/{run_id}/resume", resume_payload)
            assert r1.status_code == 202
            # 重复 resume
            r2 = await _post_signed(client, settings, f"/api/v1/runs/{run_id}/resume", resume_payload)
            assert r2.status_code in (409, 422, 400)


# ============ CRM 侧测试（需 JWT） ============
@pytest.mark.crmlive
class TestCrmSideIntegration:
    """CRM Java 集成薄层：代理创建、导入候选、确认写入。"""

    @requires_crm
    async def test_crm_proxy_create_run(self, crm_api):
        """CRM 代理创建 AI Run。"""
        payload = {"text": "今天拜访了客户测试公司张总，讨论了项目合作。下周准备报价方案。"}
        r = await crm_api.post("/api/v1/salescrm/ai/runs", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert "run_id" in body
        assert body["status"] in ("queued", "running", "waiting_input", "succeeded")

    @requires_crm
    async def test_crm_import_and_list_candidates(self, crm_api):
        """导入 Run 结果为候选 → 列表可查。"""
        # 创建 Run
        payload = {"text": "拜访客户测试A，准备方案。"}
        r = await crm_api.post("/api/v1/salescrm/ai/runs", json=payload)
        run_id = r.json()["run_id"]

        # 等 Run 完成（真实模型可能 20-30s）
        for _ in range(40):
            rr = await crm_api.get(f"/api/v1/salescrm/ai/runs/{run_id}")
            status = rr.json().get("status")
            if status in ("succeeded", "failed"):
                break
            await asyncio.sleep(1.5)

        assert status == "succeeded", f"Run 未成功: {status}"

        # 导入候选
        ir = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/import")
        assert ir.status_code == 200
        items = ir.json().get("items", [])
        assert len(items) >= 1  # 至少有活动或任务候选

        # 列表可查
        lr = await crm_api.get("/api/v1/salescrm/ai/candidates")
        assert lr.status_code == 200
        assert len(lr.json()) >= 1

    @requires_crm
    async def test_crm_import_idempotent(self, crm_api):
        """重复导入不覆盖人工编辑。"""
        # 创建 Run + 导入
        payload = {"text": "拜访客户幂等测试，准备合同。"}
        r = await crm_api.post("/api/v1/salescrm/ai/runs", json=payload)
        run_id = r.json()["run_id"]

        for _ in range(40):
            rr = await crm_api.get(f"/api/v1/salescrm/ai/runs/{run_id}")
            status = rr.json().get("status")
            if status in ("succeeded", "failed"):
                break
            await asyncio.sleep(1.5)

        assert status == "succeeded", f"Run 未成功: {status}"

        # 第一次导入
        r1 = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/import")
        items1 = r1.json().get("items", [])
        assert len(items1) >= 1

        # 第二次导入（应返回 duplicate 标记）
        r2 = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/import")
        items2 = r2.json().get("items", [])
        assert len(items2) == len(items1)
        for item in items2:
            assert item.get("duplicate") is True  # 重复导入标记

    @requires_crm
    async def test_crm_confirm_and_write(self, crm_api):
        """确认候选 → 正式活动/任务写入。"""
        payload = {"text": "拜访客户确认写入测试。"}
        r = await crm_api.post("/api/v1/salescrm/ai/runs", json=payload)
        run_id = r.json()["run_id"]

        for _ in range(40):
            rr = await crm_api.get(f"/api/v1/salescrm/ai/runs/{run_id}")
            status = rr.json().get("status")
            if status in ("succeeded", "failed"):
                break
            await asyncio.sleep(1.5)

        assert status == "succeeded", f"Run 未成功: {status}"

        # 导入
        imported = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/import")
        assert imported.status_code == 200
        item_ids = [item["item_id"] for item in imported.json().get("items", [])]
        assert item_ids, "未产生可确认候选"

        # 确认（先活动后任务）
        cr = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/confirm", json={"item_ids": item_ids})
        assert cr.status_code == 200
        results = cr.json().get("results", [])
        assert len(results) >= 1

        # 检查写入状态
        assert any(r.get("write_status") == "WRITTEN" for r in results), (
            f"所有候选均未正式写入: {results}"
        )

    @requires_crm
    async def test_crm_ignore_candidate(self, crm_api):
        """忽略候选 → 状态 IGNORED。"""
        payload = {"text": "忽略候选测试。"}
        r = await crm_api.post("/api/v1/salescrm/ai/runs", json=payload)
        run_id = r.json()["run_id"]

        for _ in range(40):
            rr = await crm_api.get(f"/api/v1/salescrm/ai/runs/{run_id}")
            status = rr.json().get("status")
            if status in ("succeeded", "failed"):
                break
            await asyncio.sleep(1.5)

        assert status == "succeeded", f"Run 未成功: {status}"

        # 导入
        ir = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/import")
        items = ir.json().get("items", [])
        assert items, "无候选可忽略"

        # 忽略第一条
        cand_id = items[0].get("id")
        r_ig = await crm_api.post(f"/api/v1/salescrm/ai/candidates/{cand_id}/ignore")
        assert r_ig.status_code == 200
        assert r_ig.json().get("status") == "IGNORED"

    @requires_crm
    async def test_crm_repeat_confirm_idempotent(self, crm_api):
        """重复确认不产生第二条正式记录。"""
        payload = {"text": "重复确认幂等测试。"}
        r = await crm_api.post("/api/v1/salescrm/ai/runs", json=payload)
        run_id = r.json()["run_id"]

        for _ in range(40):
            rr = await crm_api.get(f"/api/v1/salescrm/ai/runs/{run_id}")
            status = rr.json().get("status")
            if status in ("succeeded", "failed"):
                break
            await asyncio.sleep(1.5)

        assert status == "succeeded", f"Run 未成功: {status}"

        imported = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/import")
        assert imported.status_code == 200
        item_ids = [item["item_id"] for item in imported.json().get("items", [])]
        assert item_ids, "未产生可确认候选"

        # 第一次确认
        r1 = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/confirm", json={"item_ids": item_ids})
        assert r1.status_code == 200
        results1 = r1.json().get("results", [])
        assert any(item.get("write_status") == "WRITTEN" for item in results1), results1

        # 第二次确认（应幂等）
        r2 = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/confirm", json={"item_ids": item_ids})
        assert r2.status_code == 200
        results2 = r2.json().get("results", [])
        assert len(results1) == len(results2)

        # 同一 item 的 formal_id 应一致（幂等键保证不重复写入）
        for a, b in zip(results1, results2):
            if a.get("write_status") == "WRITTEN":
                assert a.get("formal_activity_id") == b.get("formal_activity_id")
                assert a.get("formal_task_id") == b.get("formal_task_id")


# ============ 全链路测试（AI 真实模型 + CRM） ============
@pytest.mark.crmlive
class TestFullLoop:
    """真实文字 → AI 抽取 → CRM 导入 → 确认写入，完整闭环。"""

    @requires_crm
    async def test_full_loop_real_text_to_formal_write(self, crm_api):
        """完整闭环验收：文字 → 抽取 → 导入 → 确认 → 正式活动/任务。"""
        unique_text = f"今天拜访了客户集成测试公司（{uuid.uuid4().hex[:6]}）的李总，讨论了ERP系统采购项目。下周需要提交技术方案。"
        payload = {"text": unique_text}
        r = await crm_api.post("/api/v1/salescrm/ai/runs", json=payload)
        assert r.status_code == 200
        run_id = r.json()["run_id"]

        # 等待 AI 处理
        for _ in range(60):
            rr = await crm_api.get(f"/api/v1/salescrm/ai/runs/{run_id}")
            body = rr.json()
            status = body.get("status")
            if status in ("succeeded", "failed"):
                break
            if status == "waiting_input":
                # 自动补参恢复
                pending = await crm_api.get(f"/api/v1/salescrm/ai/runs/{run_id}/pending")
                assert pending.status_code == 200 and pending.json().get("waiting")
                resumed = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/resume", json={
                    "state_version": pending.json()["state_version"],
                    "values": {"due_date": (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat(),
                               "assignee_name": "测试负责人"},
                })
                assert resumed.status_code == 200, resumed.text
            await asyncio.sleep(1.5)
        assert status == "succeeded", f"Run 未成功: {status}"

        # 导入候选
        ir = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/import")
        assert ir.status_code == 200
        items = ir.json().get("items", [])
        assert len(items) >= 1, "应至少产生 1 个候选"
        activity_items = [item for item in items if item.get("candidate_type") == "ACTIVITY"]
        task_items = [item for item in items if item.get("candidate_type") == "TASK"]
        assert activity_items and task_items, f"本样本须同时产生活动和任务候选: {items}"

        # 查询一个真实客户用于 overrides
        cust_resp = await crm_api.get("/api/v1/salescrm/customers?page=1&size=1")
        cust_id = None
        if cust_resp.status_code == 200:
            customers = cust_resp.json().get("records") or cust_resp.json().get("data") or []
            if customers:
                cust_id = customers[0].get("id")

        assert cust_id, "缺少可用于正式写入验收的有权客户"
        due_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        overrides = [
            {
                "item_id": item["item_id"],
                "customer_id": cust_id,
                "summary": unique_text,
                **({"due_at": due_at} if item["candidate_type"] == "TASK" else {}),
            }
            for item in items
        ]
        confirm_body = {"item_ids": [item["item_id"] for item in items], "items": overrides}
        cr = await crm_api.post(f"/api/v1/salescrm/ai/runs/{run_id}/confirm", json=confirm_body)
        assert cr.status_code == 200
        results = cr.json().get("results", [])

        # 正式闭环必须真的写入；全项业务失败不是通过。
        written = [r for r in results if r.get("write_status") == "WRITTEN"]
        failed = [r for r in results if r.get("write_status") == "FAILED"]
        assert results, "确认接口未返回逐项结果"
        assert len(written) + len(failed) == len(results), f"结果状态异常: {results}"
        assert any(item.get("formal_activity_id") for item in written), f"活动未正式写入: {results}"
        assert any(item.get("formal_task_id") for item in written), f"任务未正式写入: {results}"

        # 验证正式 ID 存在
        for item in written:
            assert item.get("formal_activity_id") or item.get("formal_task_id")
