"""显式初始化业务表迁移。启动只检查，不自动改库。

用法：
  python scripts/init_db.py            # 只读检查
  python scripts/init_db.py --apply    # 显式应用迁移
  可选 --config conf/config.yml / --database-url postgresql://...（覆盖配置）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.persistence import migrations  # noqa: E402
from app.persistence.pool import connect  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="sales-crm-ai 业务表迁移")
    parser.add_argument("--apply", action="store_true", help="显式应用迁移（默认只检查）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 conf/config.yml）")
    parser.add_argument("--database-url", default=None, help="覆盖配置中的 database_url")
    args = parser.parse_args()

    settings, used = Settings.load_existing(args.config)
    dsn = args.database_url or settings.database_url
    if not used and not args.database_url:
        print("未找到配置文件（conf/config.yml）且未提供 --database-url")
        return 2

    async with connect(dsn) as conn:
        if args.apply:
            applied = await migrations.apply_migrations(conn)
            if applied:
                print(f"已应用迁移: {', '.join(applied)}")
            else:
                print("无待应用迁移")
        ok, current = await migrations.check_schema_revision(conn)
        required = migrations.required_revision()
        status = "就绪" if ok else "未就绪"
        print(f"业务表结构{status}: 当前 {current} / 要求 {required}")
        return 0 if ok or args.apply else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
