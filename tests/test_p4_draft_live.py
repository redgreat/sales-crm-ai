"""M20 助手建档草稿端到端联调：AI 草稿 Run → CRM 导入（先匹配）→ 本人确认写入正式对象。

前置：
  1. AI 服务与 CRM 服务运行（本测试走 CRM 代理链：/ai/runs、/ai/runs/{id}/import-draft）
  2. 显式 SAI_RUN_CRM_LIVE=1；CRM JWT：SAI_CRM_TOKEN 或 .local/crm-token.txt

用法:
  pytest tests/test_p4_draft_live.py -q -m draftlive
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.realpg, pytest.mark.draftlive]

CRM_BASE_URL = os.environ.get("SAI_CRM_BASE_URL", "http://127.0.0.1:18080/api/v1/salescrm")
CRM_LIVE_ENABLED = os.environ.get("SAI_RUN_CRM_LIVE") == "1"
CRM_TOKEN = (os.environ.get("SAI_CRM_TOKEN", "") or (
    (Path(__file__).resolve().parent.parent / ".local" / "crm-token.txt").read_text(encoding="utf-8").strip()
    if (Path(__file__).resolve().parent.parent / ".local" / "crm-token.txt").exists() else ""
)) if CRM_LIVE_ENABLED else ""

requires_token = pytest.mark.skipif(
    not CRM_LIVE_ENABLED or not CRM_TOKEN,
    reason="真实 CRM 写入测试需显式 SAI_RUN_CRM_LIVE=1 和有效 JWT",
)


@pytest_asyncio.fixture
async def crm():
    headers = {"Authorization": CRM_TOKEN if CRM_TOKEN.startswith("Bearer") else f"Bearer {CRM_TOKEN}"}
    async with httpx.AsyncClient(base_url=CRM_BASE_URL, timeout=60, headers=headers) as client:
        yield client




async def _wait_run_succeeded(crm: httpx.AsyncClient, run_id: str, timeout: float = 120) -> dict:
    """轮询 CRM 代理的 Run 状态直到 succeeded（真实模型单次 20~25s）。"""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        resp = await crm.get(f"/ai/runs/{run_id}")
        body = resp.json() if resp.status_code == 200 else {}
        last = body
        if body.get("status") in ("succeeded", "failed", "cancelled"):
            assert body["status"] == "succeeded", f"Run 终态非成功: {body.get('error')}"
            return body
        await asyncio.sleep(3)
    raise AssertionError(f"等待 Run {run_id} 成功超时，最后状态: {last.get('status')}")


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@requires_token
async def test_customer_draft_end_to_end_writes_formal_customer(crm):
    """新增客户全链路：草稿 → 导入 → 确认 → 正式客户存在；重复确认幂等。"""
    name = _unique_name("联测客户")
    created = await crm.post("/ai/runs", json={"capability": "customer.draft", "text": f"新增客户：{name}，客户类型：ORG，经营区域：华东，业务线：车电业务线"})
    assert created.status_code in (200, 201, 202), created.text
    run_id = created.json()["run_id"]
    await _wait_run_succeeded(crm, run_id)

    imported = await crm.post(f"/ai/runs/{run_id}/import-draft")
    assert imported.status_code in (200, 201), imported.text
    candidate = imported.json()
    assert candidate["candidate_type"] == "CUSTOMER"
    assert candidate["match_status"] == "UNMATCHED"

    confirmed = await crm.post(f"/ai/candidates/{candidate['id']}/confirm-draft",
                               json={"similar_continue_reason": "AI 联测数据，确认为新客户"})
    assert confirmed.status_code in (200, 201), confirmed.text
    body = confirmed.json()
    assert body["write_status"] == "WRITTEN"
    formal_id = body["formal_id"]
    assert formal_id

    # 弱网重复确认：幂等返回原对象（M-07）
    replay = await crm.post(f"/ai/candidates/{candidate['id']}/confirm-draft", json={})
    assert replay.status_code in (200, 201)
    assert replay.json().get("idempotent_replay") is True
    assert replay.json().get("formal_id") == formal_id


@requires_token
async def test_duplicate_customer_name_is_rejected_with_hint(crm):
    """先匹配再新增：同名客户确认被拒绝并给出「改为补增」提示（M20 红线）。"""
    existing_name = "上海澜途汽车服务有限公司"  # zrcrm 库真实客户
    created = await crm.post("/ai/runs", json={"capability": "customer.draft", "text": f"新增客户：{existing_name}，客户类型：ORG，经营区域：华东，业务线：车电业务线"})
    run_id = created.json()["run_id"]
    await _wait_run_succeeded(crm, run_id)
    imported = await crm.post(f"/ai/runs/{run_id}/import-draft")
    candidate = imported.json()
    assert candidate["match_status"] == "MATCHED"
    assert candidate.get("match_hint"), "必须给「改为补增」出口提示"

    confirmed = await crm.post(f"/ai/candidates/{candidate['id']}/confirm-draft", json={})
    # 业务拒绝：明确错误而非静默建第二条档案
    assert confirmed.status_code >= 400 or confirmed.json().get("write_status") == "FAILED"


@requires_token
async def test_lead_draft_end_to_end(crm):
    """登记线索：草稿 → 导入 → 确认 → 正式线索（来源 LS-MANUAL，幂等键 ai:run:item）。"""
    content = f"联测线索-{uuid.uuid4().hex[:8]}：客户想了解仓储改造方案"
    created = await crm.post("/ai/runs", json={"capability": "lead.draft", "text": f"登记线索：{content}，联系人：测试，电话：13800001111"})
    run_id = created.json()["run_id"]
    await _wait_run_succeeded(crm, run_id)
    imported = await crm.post(f"/ai/runs/{run_id}/import-draft")
    candidate = imported.json()
    assert candidate["candidate_type"] == "LEAD"
    confirmed = await crm.post(f"/ai/candidates/{candidate['id']}/confirm-draft",
                               json={"similar_continue_reason": "AI 联测数据，确认为新线索"})
    assert confirmed.status_code in (200, 201), confirmed.text
    body = confirmed.json()
    assert body["write_status"] == "WRITTEN" and body["formal_id"]
