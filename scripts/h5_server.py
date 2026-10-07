"""测试用 H5 联调端本地服务：静态托管 + 账号密码登录 + CRM 同源代理。

解决的两个实际问题：
  1. 浏览器不再需要手工粘贴 JWT——密码授权（password grant）在服务端完成，
     OAuth client_secret 不出本机、不进浏览器、不进 Git。
  2. CRM 的 CORS 白名单只有 http://localhost:81 与 http://127.0.0.1:81（见
     sales-crm-api-service MvcConfiguration），file:// 打开或任意端口页面会被浏览器拦截；
     本服务把 /crm/** 同源代理到 CRM，页面所有调用都是同源，不再受该限制。

用法：
  .venv/Scripts/python.exe scripts/h5_server.py --port 8320 \
      --crm-base http://127.0.0.1:18080/api/v1/salescrm [--open]

然后浏览器打开 http://127.0.0.1:8320/ ，填工号/密码点登录即可。

凭据来源（按优先级；**本脚本内不含任何密钥**）：
  1) 环境变量：SAI_CRM_CLIENT_ID / SAI_CRM_CLIENT_SECRET /
     SAI_CRM_TOKEN_ENDPOINT / SAI_CRM_SCOPES
  2) .local/identity.json：{"client_id","client_secret","token_endpoint","scopes"}
  3) scripts/get_crm_token.py 内的常量（该文件因含 secret 未入库，存在时复用）

token 仍落在 .local/crm-token.txt（与 get_crm_token.py、pytest 用例共用），
密码只保留在本进程内存中用于自动续期，绝不落盘。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
H5_DIR = PROJECT_ROOT / "tests" / "h5"
TOKEN_PATH = PROJECT_ROOT / ".local" / "crm-token.txt"
IDENTITY_PATH = PROJECT_ROOT / ".local" / "identity.json"

# 非敏感的默认值（与 CRM application-dev.yml 中的公开 client-id 一致）
DEFAULT_TOKEN_ENDPOINT = "https://identity-fat.lunz.cn/connect/token"
DEFAULT_CLIENT_ID = "sales-crm-client"
DEFAULT_SCOPES = "sales-crm app-4754bb86-d82e-400c-9e68-936c4a195c8d uc-users-outside-api"

REFRESH_MARGIN_SECONDS = 120  # 剩余有效期低于此值时自动续期
LOCK = threading.Lock()


# --------------------------------------------------------------------------- 凭据

def load_identity_config() -> dict:
    cfg = {
        "token_endpoint": os.environ.get("SAI_CRM_TOKEN_ENDPOINT", DEFAULT_TOKEN_ENDPOINT),
        "client_id": os.environ.get("SAI_CRM_CLIENT_ID", DEFAULT_CLIENT_ID),
        "client_secret": os.environ.get("SAI_CRM_CLIENT_SECRET", ""),
        "scopes": os.environ.get("SAI_CRM_SCOPES", DEFAULT_SCOPES),
    }
    if IDENTITY_PATH.exists():
        try:
            file_cfg = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # 配置损坏不静默：明确报错
            raise SystemExit(f"[x] {IDENTITY_PATH} 解析失败：{exc}")
        for key in ("token_endpoint", "client_id", "client_secret", "scopes"):
            if file_cfg.get(key):
                cfg[key] = file_cfg[key]
    if not cfg["client_secret"]:
        # 回退到本地未入库脚本中的常量（老环境遗留，含 secret）
        token_script = PROJECT_ROOT / "scripts" / "get_crm_token.py"
        if token_script.exists():
            ns: dict = {}
            try:
                exec(compile(token_script.read_text(encoding="utf-8"), str(token_script), "exec"),
                     {"__name__": "get_crm_token", "__file__": str(token_script)}, ns)
            except Exception:
                ns = {}
            cfg["client_secret"] = ns.get("CLIENT_SECRET", "")
            cfg["client_id"] = ns.get("CLIENT_ID", cfg["client_id"])
            cfg["token_endpoint"] = ns.get("TOKEN_ENDPOINT", cfg["token_endpoint"])
            cfg["scopes"] = ns.get("SCOPES", cfg["scopes"])
    if not cfg["client_secret"]:
        raise RuntimeError(
            "缺少 OAuth client_secret：请设置 SAI_CRM_CLIENT_SECRET 环境变量，"
            f"或写入 {IDENTITY_PATH}（该目录已 gitignore）。"
        )
    return cfg


# --------------------------------------------------------------------------- token

def jwt_claims(token: str) -> dict:
    """解出 JWT payload（不校验签名，仅用于判断过期与展示归属账号）。"""
    raw = token.split(" ", 1)[1] if token.lower().startswith("bearer ") else token
    try:
        payload = raw.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def jwt_exp(token: str) -> int:
    return int(jwt_claims(token).get("exp", 0))


def jwt_username(token: str) -> str:
    claims = jwt_claims(token)
    for key in ("name", "unique_name", "preferred_username", "staff_no", "sub"):
        if claims.get(key):
            return str(claims[key])
    return ""


def normalize(token: str) -> str:
    token = (token or "").strip().strip('"')
    return token if token.lower().startswith("bearer ") else f"Bearer {token}" if token else ""


class Session:
    """进程内会话：token + 可选密码（仅内存，用于自动续期）。"""

    def __init__(self) -> None:
        self.token = ""
        self.username = ""
        self.password: str | None = None  # 只在用户勾选「保持登录」时保留在内存
        self.expires_at = 0
        self.source = ""
        self._load_saved_token()

    def _load_saved_token(self) -> None:
        if TOKEN_PATH.exists():
            token = normalize(TOKEN_PATH.read_text(encoding="utf-8"))
            if token:
                self.token = token
                self.expires_at = jwt_exp(token)
                self.source = "file"
                # 从文件恢复时无法得知登录工号，用 JWT 内的账号名兜底展示（便于发现 token 归属错误）
                self.username = self.username or jwt_username(token)

    def reload_saved(self) -> bool:
        """退出登录后重新读取本机 token 文件（其他脚本可能刚刷新过）。"""
        self._load_saved_token()
        return self.valid()

    def set_token(self, token: str, username: str = "", source: str = "login") -> None:
        self.token = normalize(token)
        self.username = username or self.username
        self.expires_at = jwt_exp(self.token)
        self.source = source
        try:
            TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(self.token, encoding="utf-8")
        except OSError as exc:
            print(f"[warn] token 保存失败（不影响本次会话）：{exc}")

    def remaining(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def valid(self) -> bool:
        return bool(self.token) and self.remaining() > 0

    def view(self) -> dict:
        return {
            "connected": self.valid(),
            "username": self.username,
            "expires_at": self.expires_at,
            "remaining_seconds": self.remaining(),
            "source": self.source,
            "token_preview": (self.token[:16] + "…") if self.token else "",
        }


SESSION = Session()


def password_grant(username: str, password: str) -> dict:
    cfg = load_identity_config()
    data = {
        "grant_type": "password",
        "username": username,
        "password": password,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "scope": cfg["scopes"],
    }
    with _client(cfg["token_endpoint"], 20) as client:
        resp = client.post(cfg["token_endpoint"], data=data)
    if resp.status_code != 200:
        raise RuntimeError(f"登录失败 {resp.status_code}：{resp.text[:200]}")
    body = resp.json()
    return {"token": body["access_token"], "expires_in": int(body.get("expires_in", 0))}


def ensure_fresh_token() -> str:
    """返回可用 token；临近过期且持有密码时自动续期。"""
    with LOCK:
        if SESSION.valid() and SESSION.remaining() > REFRESH_MARGIN_SECONDS:
            return SESSION.token
        if SESSION.password and SESSION.username:
            result = password_grant(SESSION.username, SESSION.password)
            SESSION.set_token(result["token"], SESSION.username, source="refresh")
            return SESSION.token
        return SESSION.token if SESSION.valid() else ""


# --------------------------------------------------------------------------- HTTP

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def _client(base_url: str, timeout: float) -> httpx.Client:
    """回环地址直连，不经开发机代理（代理会拒绝 127.0.0.1 上游）；外网地址沿用环境代理。"""
    host = (urlsplit(base_url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
        return httpx.Client(timeout=timeout, trust_env=False)
    return httpx.Client(timeout=timeout)


def _safe_base_override(value: str, default: str) -> str:
    """允许页面临时指定 CRM 地址，但只允许本机回环地址（避免被拿去打任意内网）。"""
    if not value:
        return default
    parts = urlsplit(value)
    if parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost", "[::1]"):
        return value.rstrip("/")
    return default


class Handler(BaseHTTPRequestHandler):
    server_version = "h5-dev-server"
    crm_base = "http://127.0.0.1:18080/api/v1/salescrm"

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers
    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- routes
    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/crm/"):
            return self._proxy("GET")
        if path in ("/", "/index.html", "/h5", "/h5/"):
            return self._static("index.html")
        if path == "/__session":
            return self._json(200, SESSION.view())
        return self._static(path.lstrip("/"))

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/crm/"):
            return self._proxy("POST")
        body = self._json_body()
        if path == "/__login":
            return self._login(body)
        if path == "/__logout":
            SESSION.password = None
            SESSION.token = ""
            SESSION.expires_at = 0
            SESSION.source = ""
            return self._json(200, {"ok": True})
        return self._json(404, {"ok": False, "error": "not found"})

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_PATCH(self):
        self._proxy("PATCH")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.end_headers()

    def _login(self, body: dict):
        # 应急通道：直接注入已有 token（不走密码授权）
        manual = (body.get("token") or "").strip()
        if manual:
            SESSION.set_token(manual, (body.get("username") or "").strip(), source="manual")
            if not SESSION.valid():
                return self._json(400, {"ok": False, "error": "token 无法解析或已过期"})
            return self._json(200, {"ok": True, **SESSION.view()})
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        keep = bool(body.get("keep_signed_in", True))
        # 无账号密码 → 复用本机已保存且未过期的 token（退出登录后也允许重新读取文件）
        if not username and not password:
            if not SESSION.valid():
                SESSION.reload_saved()
            if SESSION.valid():
                return self._json(200, {"ok": True, **SESSION.view(), "reused": True})
            return self._json(401, {"ok": False, "error": "本地 token 缺失或已过期，请用账号密码登录"})
        if not password:
            return self._json(400, {"ok": False, "error": "请输入密码"})
        try:
            result = password_grant(username, password)
        except Exception as exc:
            return self._json(401, {"ok": False, "error": str(exc)})
        SESSION.set_token(result["token"], username, source="login")
        SESSION.password = password if keep else None
        return self._json(200, {"ok": True, **SESSION.view(), "expires_in": result["expires_in"]})

    def _static(self, rel: str):
        target = (H5_DIR / rel).resolve()
        if not target.is_relative_to(H5_DIR.resolve()) or not target.is_file():
            return self._json(404, {"ok": False, "error": "not found"})
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ---- CRM 同源代理
    def _proxy(self, method: str):
        split = urlsplit(self.path)
        if not split.path.startswith("/crm/"):
            return self._json(404, {"ok": False, "error": "not found"})
        token = ensure_fresh_token()
        if not token:
            return self._json(401, {"ok": False, "error": "未登录或 token 已过期，请先登录"})
        base = _safe_base_override(self.headers.get("X-CRM-Base", ""), self.crm_base)
        url = base + split.path[len("/crm"):] + (f"?{split.query}" if split.query else "")
        headers = {
            "Authorization": token,
            "Accept": self.headers.get("Accept", "application/json"),
        }
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]
        try:
            with _client(base, 60) as client:
                resp = client.request(method, url, headers=headers,
                                      content=self._body() or None, follow_redirects=False)
        except httpx.HTTPError as exc:
            return self._json(502, {"ok": False, "error": f"CRM 不可达：{exc}"})
        payload = resp.content
        self.send_response(resp.status_code)
        content_type = resp.headers.get("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="测试用 H5 联调端本地服务")
    parser.add_argument("--port", type=int, default=8320)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--crm-base", default=os.environ.get(
        "SAI_CRM_BASE_URL", "http://127.0.0.1:18080/api/v1/salescrm"))
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    if not H5_DIR.exists():
        raise SystemExit(f"[x] 找不到 H5 目录：{H5_DIR}")

    try:
        load_identity_config()
        print("[ok] OAuth 凭据：已就绪（环境变量 / .local/identity.json）")
    except RuntimeError as exc:
        print(f"[warn] {exc}——仅能复用已存在的本地 token，账号密码登录会失败")

    Handler.crm_base = args.crm_base.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[ok] H5 联调端：{url}")
    print(f"[ok] CRM 代理：{url}crm/** -> {Handler.crm_base}/**")
    print(f"[ok] 本地 token：{TOKEN_PATH}"
          f"{'（已存在，可直接复用）' if TOKEN_PATH.exists() else '（尚未生成，需账号密码登录）'}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ok] 已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
