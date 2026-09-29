"""获取 CRM JWT token 并保存供测试使用。

用法:
  # 方式1: 密码登录（推荐，自动获取）
  python scripts/get_crm_token.py --username 工号 --password 密码

  # 方式2: 手动粘贴 token
  python scripts/get_crm_token.py --token "Bearer eyJ..."

  # 方式3: 从浏览器复制的 Authorization 头粘贴到文件
  # 直接把 token 保存到 .local/crm-token.txt

Token 会保存到 .local/crm-token.txt，测试自动读取。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

TOKEN_PATH = Path(__file__).resolve().parent.parent / ".local" / "crm-token.txt"

# 从 application-dev.yml 提取的 OAuth2 配置
TOKEN_ENDPOINT = "https://identity-fat.lunz.cn/connect/token"
CLIENT_ID = "sales-crm-client"
CLIENT_SECRET = "QhxzV5NofVtF0SduoKrfP4ET7FhpYSFX"
SCOPES = "sales-crm app-4754bb86-d82e-400c-9e68-936c4a195c8d uc-users-outside-api"


def save_token(token: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token.strip(), encoding="utf-8")
    print(f"[ok] Token 已保存到 {TOKEN_PATH}")


def get_token_by_password(username: str, password: str) -> str:
    """Resource Owner Password Credentials grant."""
    data = {
        "grant_type": "password",
        "username": username,
        "password": password,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": SCOPES,
    }
    print(f"[...] 正在登录 {username} ...")
    with httpx.Client(timeout=15) as client:
        resp = client.post(TOKEN_ENDPOINT, data=data)
        if resp.status_code != 200:
            print(f"[x] 登录失败: {resp.status_code}")
            print(resp.text[:500])
            sys.exit(1)
        body = resp.json()
        token = "Bearer " + body["access_token"]
        expires_in = body.get("expires_in", 0)
        print(f"[ok] 登录成功，token 有效期 {expires_in}s")
        return token


def get_token_client_credentials() -> str:
    """Client Credentials grant（无用户身份，仅用于调试）。"""
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "sales-crm",
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(TOKEN_ENDPOINT, data=data)
        resp.raise_for_status()
        body = resp.json()
        return "Bearer " + body["access_token"]


def main():
    parser = argparse.ArgumentParser(description="获取 CRM JWT token")
    parser.add_argument("--username", help="登录用户名（工号）")
    parser.add_argument("--password", help="登录密码")
    parser.add_argument("--token", help="直接提供 Authorization 头值")
    args = parser.parse_args()

    if args.token:
        token = args.token.strip()
        if not token.startswith("Bearer "):
            token = "Bearer " + token
        save_token(token)
    elif args.username and args.password:
        token = get_token_by_password(args.username, args.password)
        save_token(token)
    else:
        # 从标准输入读取（交互式粘贴）
        print("请粘贴 Authorization 头值（Bearer eyJ...），按回车确认：")
        token = input().strip()
        if token:
            if not token.startswith("Bearer "):
                token = "Bearer " + token
            save_token(token)
        else:
            print("[x] 未提供 token")
            sys.exit(1)

    # 验证 token 可用
    print("[...] 验证 token ...")
    with httpx.Client(timeout=10) as client:
        resp = client.get(
            "http://127.0.0.1:8080/api/v1/salescrm/ai/candidates",
            headers={"Authorization": token},
        )
        if resp.status_code == 200:
            print("[ok] Token 验证通过！可以运行测试了。")
            print("  运行: pytest tests/test_crm_integration.py -v")
        elif resp.status_code == 401:
            print("[x] Token 无效或已过期")
            sys.exit(1)
        elif resp.status_code == 403:
            print("[warn] Token 可用但用户未映射 ZR 工号（client_credentials token）")
            print("       需要用户登录 token 才能测试候选导入/确认")
        else:
            print(f"[?] 未知状态: {resp.status_code} {resp.text[:200]}")


if __name__ == "__main__":
    main()
