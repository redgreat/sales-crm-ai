"""显式初始化 LangGraph checkpoint 结构（AsyncPostgresSaver.setup）。

启动只检查，不自动改库；本脚本是与 AgentZR 一致的显式入口
（机制改造自 AgentZR shared/persistence/migrations/checkpoints.py，提交 b14adec）。

用法：
  python scripts/init_checkpoints.py            # 只读检查
  python scripts/init_checkpoints.py --apply    # 显式建表
  可选 --config conf/config.yml / --database-url postgresql://...（覆盖配置）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from psycopg import AsyncConnection  # noqa: E402

from app.config import Settings  # noqa: E402
from app.persistence.checkpoints import check_checkpoint_schema  # noqa: E402

LOCK_KEY = 786543302


async def apply(dsn: str) -> None:
    conn = await AsyncConnection.connect(dsn, autocommit=True)
    try:
        async with conn.transaction():
            await conn.execute(f"SELECT pg_advisory_xact_lock({LOCK_KEY})")
            async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                await saver.setup()
    finally:
        await conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="sales-crm-ai checkpoint 初始化")
    parser.add_argument("--apply", action="store_true", help="显式建表（默认只检查）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 conf/config.yml）")
    parser.add_argument("--database-url", default=None, help="覆盖配置中的 database_url")
    args = parser.parse_args()

    settings, used = Settings.load_existing(args.config)
    dsn = args.database_url or settings.database_url
    if not used and not args.database_url:
        print("未找到配置文件（conf/config.yml）且未提供 --database-url")
        return 2

    ok, version = await check_checkpoint_schema(dsn)
    expected = len(AsyncPostgresSaver.MIGRATIONS) - 1
    if args.apply:
        await apply(dsn)
        ok, version = await check_checkpoint_schema(dsn)
        print("checkpoint 结构已初始化")
    status = "就绪" if ok else "未就绪"
    print(f"checkpoint 结构{status}: 数据库版本 {version} / 期望 {expected}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
