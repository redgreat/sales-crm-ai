"""读取配置值（供 dev.ps1 等脚本使用，避免 PowerShell 解析 YAML）。

用法：
  python scripts/print_config.py database_url
  python scripts/print_config.py api.port
  python scripts/print_config.py --file        # 输出实际使用的配置文件路径
  python scripts/print_config.py --exists      # 退出码 0/1 表示配置文件是否存在
"""
from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="读取 sales-crm-ai 配置值")
    parser.add_argument("key", nargs="?", help="配置键，支持点号嵌套，如 api.port")
    parser.add_argument("--config", default=None, help="显式配置文件路径")
    parser.add_argument("--file", action="store_true", help="输出实际使用的配置文件路径")
    parser.add_argument("--exists", action="store_true", help="仅判断配置文件是否存在")
    args = parser.parse_args()

    if args.exists:
        _, used = Settings.load_existing(args.config)
        return 0 if used else 1

    settings, used = Settings.load_existing(args.config)
    if args.file:
        print(str(used) if used else "")
        return 0

    if not args.key:
        parser.error("需要 key 或 --file/--exists")
    value: object = settings
    for part in args.key.split("."):
        value = getattr(value, part)
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
