"""AI 服务启动入口（uvicorn）。

Windows 上 psycopg 异步模式要求 SelectorEventLoop；先用策略再启动
（loop="none" 让 uvicorn 使用本进程循环，不在 Windows 重新建 Proactor 循环）。
Linux/macOS 行为不变。

用法：python scripts/serve.py [--host H] [--port P]
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402

from app.config import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="sales-crm-ai API 服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = get_settings()

    config = uvicorn.Config(
        "app.api.main:app",
        host=args.host or settings.api.host,
        port=args.port or settings.api.port,
        loop="none",
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
