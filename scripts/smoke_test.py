"""端到端冒烟测试：初始化脚本 + uvicorn 服务 + HTTP API 全流程。

用法：python scripts/smoke_test.py
自起临时 PostgreSQL（复用 tests/conftest 的 PgCluster），验证：
  1. scripts/init_db.py --apply 与 scripts/init_checkpoints.py --apply
  2. uvicorn 启动后 /health /ready
  3. 签名请求创建 Run → worker 执行 → 结果持久化
  4. 重复 nonce 被拒（防重放）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from app.auth import OperatorContext, sign_request  # noqa: E402


def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None):
    """返回 (status, body)；非 2xx 也返回状态码（由断言判断，不抛异常）。"""
    import urllib.error

    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}


def main() -> int:
    from conftest import PgCluster, _find_pg_bin  # type: ignore[import-not-found]

    bin_dir = _find_pg_bin()
    if bin_dir is None:
        print("SKIP: 找不到 PostgreSQL 二进制")
        return 0
    cluster = PgCluster.provision(bin_dir, PROJECT_ROOT / ".local" / "pg-smoke")
    venv_python = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
    cluster.create_database("sales_crm_ai")
    db_url = cluster.url("sales_crm_ai")
    api_port = 8319
    base = f"http://127.0.0.1:{api_port}"
    process = None
    try:
        # 1. 显式初始化脚本
        for script in ("scripts/init_db.py", "scripts/init_checkpoints.py"):
            result = subprocess.run(
                [venv_python, script, "--apply", "--database-url", db_url],
                capture_output=True, text=True, cwd=PROJECT_ROOT,
            )
            assert result.returncode == 0, f"{script} 失败: {result.stdout}{result.stderr}"
            print(f"[ok] {script}: {result.stdout.strip().splitlines()[-1]}")

        # 2. 启动服务（Stub Provider 显式启用 + playground 开启；配置经 SAI_CONFIG 注入）
        config_path = PROJECT_ROOT / ".local" / "smoke-config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "environment: dev\n"
            f"database_url: {db_url}\n"
            "model:\n  provider: stub\n"
            "auth:\n  service_key_id: crm-ai\n  service_secret: smoke-secret\n"
            "worker:\n  enabled: true\n  concurrency: 1\n"
            "  poll_interval_seconds: 0.1\n"
            "api:\n  host: 127.0.0.1\n"
            f"  port: {api_port}\n"
            "  playground:\n    enabled: true\n"
            "log_level: INFO\n",
            encoding="utf-8",
        )
        env = dict(os.environ, SAI_CONFIG=str(config_path))
        log = open(PROJECT_ROOT / ".local" / "smoke-api.log", "wb")
        process = subprocess.Popen(
            [venv_python, "scripts/serve.py", "--port", str(api_port)],
            cwd=PROJECT_ROOT, stdout=log, stderr=log, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(60):
            try:
                status, _ = http("GET", f"{base}/health")
                if status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        status, body = http("GET", f"{base}/ready")
        assert status == 200, f"/ready 未就绪: {body}"
        print(f"[ok] /health /ready: {body}")

        # 3. 签名请求创建 Run → 等待完成
        payload = {"capability": "communication.extract",
                   "input": {"text": "客户：ACME公司\n任务：电话回访\n负责人：张三\n2026-10-01"},
                   "idempotency_key": f"smoke-{int(time.time())}"}
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = sign_request(secret="smoke-secret", key_id="crm-ai", method="POST",
                               path="/api/v1/runs", body=body_bytes,
                               operator=OperatorContext(user_id="smoker", user_name="冒烟测试"))
        headers["Content-Type"] = "application/json"
        status, run = http("POST", f"{base}/api/v1/runs", headers, body_bytes)
        assert status == 202, run
        run_id = run["run_id"]
        print(f"[ok] 创建 Run: {run_id} ({run['status']})")

        for _ in range(60):
            headers_get = sign_request(secret="smoke-secret", key_id="crm-ai", method="GET",
                                       path=f"/api/v1/runs/{run_id}",
                                       operator=OperatorContext(user_id="smoker"))
            status, run = http("GET", f"{base}/api/v1/runs/{run_id}", headers_get)
            if run["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.5)
        assert run["status"] == "succeeded", run
        task = run["result"]["candidates"]["tasks"][0]
        assert task["due_date"] == "2026-10-01"
        print(f"[ok] Run 完成: 任务候选「{task['title']}」截止 {task['due_date']}")

        # 4. 防重放：新签名可访问；同 nonce 重放被拒
        fresh = sign_request(secret="smoke-secret", key_id="crm-ai", method="GET",
                             path=f"/api/v1/runs/{run_id}",
                             operator=OperatorContext(user_id="smoker"))
        status, _ = http("GET", f"{base}/api/v1/runs/{run_id}", fresh)
        assert status == 200, f"新签名请求应 200，实际 {status}"
        replay_headers = sign_request(secret="smoke-secret", key_id="crm-ai", method="GET",
                                      path=f"/api/v1/runs/{run_id}",
                                      operator=OperatorContext(user_id="smoker"), nonce="fixed-nonce")
        status, _ = http("GET", f"{base}/api/v1/runs/{run_id}", replay_headers)
        assert status == 200, f"首次使用 nonce 应 200，实际 {status}"
        status, _ = http("GET", f"{base}/api/v1/runs/{run_id}", replay_headers)
        assert status == 401, f"重放应 401，实际 {status}"
        print("[ok] 防重放: 相同 nonce 二次请求被 401 拒绝")

        print("\nSMOKE PASS")
        return 0
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except Exception:
                process.kill()
        cluster.stop(mode="immediate")


if __name__ == "__main__":
    raise SystemExit(main())
