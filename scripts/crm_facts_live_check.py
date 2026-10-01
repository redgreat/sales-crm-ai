"""P4 真实联调：AI → CRM 事实查询端到端验证（HMAC 签名 + 真实 zrcrm 库）。

前提：CRM 应用已启动（本地 dev 配置），本脚本不需要 CRM JWT（facts 端点走 HMAC 服务签名）。

用法:
  python scripts/crm_facts_live_check.py \
    --base-url http://127.0.0.1:18080/api/v1/salescrm \
    --key-id crm-ai --secret <CRM侧AI_SERVICE_AUTH_SECRET>

覆盖场景：
  1. customer facts 真实对象（unrestricted 角色）→ found=true 且字段齐全
  2. 数据范围：普通销售查无权客户 → 不越权（found=false 或 403）
  3. lead / opportunity facts
  4. 不存在对象 → found=false
  5. 未知 subjectType → 400（契约：显式报错而非静默）
  6. 错误 secret → 401
  7. nonce 重放 → 401
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac as hmac_mod
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import OperatorContext, sign_request  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(f"{name}: {detail}")
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def _signed_get(base_url: str, path: str, secret: str, key_id: str,
                      operator: OperatorContext, *, nonce: str | None = None,
                      timestamp: str | None = None) -> httpx.Response:
    # 签名 path 口径：完整 URI（含 base_url 的应用前缀），与 CRM 验签 getRequestURI 一致
    from urllib.parse import urlparse
    prefix = urlparse(base_url).path.rstrip("/")
    headers = sign_request(secret=secret, key_id=key_id, method="GET", path=f"{prefix}{path}",
                           body=b"", operator=operator, nonce=nonce, timestamp=timestamp)
    async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
        return await client.get(path, headers=headers)


async def main() -> int:
    parser = argparse.ArgumentParser(description="CRM facts 真实联调检查")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-id", default="crm-ai")
    parser.add_argument("--secret", required=True)
    args = parser.parse_args()

    secret, key_id = args.secret, args.key_id
    unrestricted = OperatorContext(user_id="ST0000000006", user_name="孙管理")  # R5
    plain_sales = OperatorContext(user_id="ST0000000002", user_name="林晓")      # R1

    # 1. customer facts（真实对象，unrestricted 角色）
    r = await _signed_get(args.base_url, "/ai/integration/facts/customer/CU0000000002",
                          secret, key_id, unrestricted)
    body = r.json()
    check("customer.found", r.status_code == 200 and body.get("found") is True,
          f"status={r.status_code} found={body.get('found')} name={body.get('facts', {}).get('name')}")
    check("customer.fields", isinstance(body.get("facts"), dict)
          and {"id", "name", "master_status"} <= set(body.get("facts", {})),
          f"keys={sorted(body.get('facts', {}).keys())[:6]}...")

    # 2. 数据范围：普通销售查非本人客户——不得返回明细（found=false 或 403 均为不越权）
    r2 = await _signed_get(args.base_url, "/ai/integration/facts/customer/CU0000000002",
                           secret, key_id, plain_sales)
    body2 = r2.json() if r2.status_code == 200 else {}
    check("customer.data_scope", r2.status_code == 403 or body2.get("found") is False,
          f"status={r2.status_code} found={body2.get('found')}")

    # 3/4. lead 与 opportunity（存在与否只验证契约结构；不存在也必须 found=false 而非 500）
    for subject, sid in (("lead", "LD0000000001"), ("opportunity", "OP0000000001")):
        r3 = await _signed_get(args.base_url, f"/ai/integration/facts/{subject}/{sid}",
                               secret, key_id, unrestricted)
        body3 = r3.json() if r3.status_code == 200 else {}
        ok = r3.status_code in (200, 403) and (r3.status_code == 403 or
                                               body3.get("subject_type") == subject and
                                               body3.get("subject_id") == sid)
        check(f"{subject}.contract_shape", ok,
              f"status={r3.status_code} found={body3.get('found')}")

    # 5. 不存在对象 → found=false
    r4 = await _signed_get(args.base_url, "/ai/integration/facts/customer/CU9999999999",
                           secret, key_id, unrestricted)
    check("customer.not_found", r4.status_code == 200 and r4.json().get("found") is False,
          f"status={r4.status_code}")

    # 6. 未知 subjectType → 400
    r5 = await _signed_get(args.base_url, "/ai/integration/facts/robot/R001",
                           secret, key_id, unrestricted)
    check("unknown.subject_type", r5.status_code == 400, f"status={r5.status_code}")

    # 7. 错误 secret → 401
    r6 = await _signed_get(args.base_url, "/ai/integration/facts/customer/CU0000000002",
                           "wrong-secret", key_id, unrestricted)
    check("bad.secret", r6.status_code == 401, f"status={r6.status_code}")

    # 8. nonce 重放 → 401（同一 nonce + timestamp 第二次发送）
    path = "/ai/integration/facts/customer/CU0000000002"
    ts = str(int(time.time()))
    first = await _signed_get(args.base_url, path, secret, key_id, unrestricted,
                              nonce="replaynonce0001", timestamp=ts)
    second = await _signed_get(args.base_url, path, secret, key_id, unrestricted,
                               nonce="replaynonce0001", timestamp=ts)
    check("nonce.replay", second.status_code == 401,
          f"first={first.status_code} second={second.status_code}")

    print(f"\n===== {len(PASSED)} passed, {len(FAILED)} failed =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
