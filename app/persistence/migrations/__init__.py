"""业务表迁移：advisory lock + schema_migrations 台账，显式执行。

服务启动只读校验（check_schema_revision），绝不自动改库（需求 5.1/第 8 节）。
"""
from __future__ import annotations

import re
from pathlib import Path

import psycopg

MIGRATION_LOCK_KEY = 786543301
MIGRATIONS_DIR = Path(__file__).parent / "versions"
_FILENAME_RE = re.compile(r"^(\d{4}_[a-z0-9_]+)\.sql$")


def list_migrations() -> list[tuple[str, str]]:
    """返回 [(revision, sql)]，按版本号排序。"""
    items: list[tuple[str, str]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise RuntimeError(f"迁移文件命名不合规: {path.name}")
        items.append((match.group(1), path.read_text(encoding="utf-8")))
    return items


def required_revision() -> str:
    migrations = list_migrations()
    if not migrations:
        raise RuntimeError("没有任何迁移版本")
    return migrations[-1][0]


async def applied_revisions(conn: psycopg.AsyncConnection) -> list[str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT revision FROM schema_migrations ORDER BY revision"
        )
        return [row[0] for row in await cur.fetchall()]


async def current_revision(conn: psycopg.AsyncConnection) -> str | None:
    revisions = await applied_revisions(conn)
    return revisions[-1] if revisions else None


async def check_schema_revision(conn: psycopg.AsyncConnection) -> tuple[bool, str | None]:
    """只读校验：返回 (是否满足要求, 当前版本)。"""
    try:
        current = await current_revision(conn)
    except psycopg.errors.UndefinedTable:
        return False, None
    return current == required_revision(), current


async def apply_migrations(conn: psycopg.AsyncConnection) -> list[str]:
    """显式执行：加 advisory lock，逐版本事务内应用。返回本次应用的版本。"""
    applied_now: list[str] = []
    async with conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_KEY})")
        async with conn.cursor() as cur:
            await cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (revision TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        done = set(await applied_revisions(conn))
        for revision, sql in list_migrations():
            if revision in done:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (revision) VALUES (%s)", (revision,)
                )
            applied_now.append(revision)
    return applied_now
