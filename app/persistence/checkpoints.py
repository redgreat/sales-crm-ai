"""LangGraph PostgreSQL checkpoint 连接管理。

改造自 AgentZR shared/persistence/checkpoints.py（提交 b14adec，Apache-2.0）：
psycopg3 连接、autocommit、prepare_threshold=0；只读结构校验，失败不降级
为无状态图；连接生命周期由调用方通过 asynccontextmanager 持有。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

CHECKPOINT_MIGRATIONS_TABLE = "checkpoint_migrations"


class CheckpointSchemaNotReady(RuntimeError):
    """checkpoint 结构未初始化或版本不匹配；必须显式运行初始化脚本。"""


async def _open_connection(conn_string: str) -> AsyncConnection:
    conn = await AsyncConnection.connect(
        conn_string,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )
    return conn


async def check_checkpoint_schema(conn_string: str) -> tuple[bool, str | None]:
    """只读校验 checkpoint 表与迁移版本（不执行 DDL）。"""
    expected = len(AsyncPostgresSaver.MIGRATIONS) - 1
    conn = await _open_connection(conn_string)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s) AS table_name",
                (CHECKPOINT_MIGRATIONS_TABLE,),
            )
            row = await cursor.fetchone()
            if not row or not row["table_name"]:
                return False, None
            # langgraph-checkpoint-postgres 3.x：台账列为 v
            await cursor.execute("SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1")
            row = await cursor.fetchone()
            version = row["v"] if row else None
            return version == expected, version
    finally:
        await conn.close()


@asynccontextmanager
async def open_postgres_saver(conn_string: str) -> AsyncIterator[AsyncPostgresSaver]:
    """打开 checkpoint saver；结构不满足时抛错（不静默降级无状态）。"""
    conn = await _open_connection(conn_string)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s) AS table_name",
                (CHECKPOINT_MIGRATIONS_TABLE,),
            )
            row = await cursor.fetchone()
            if not row or not row["table_name"]:
                raise CheckpointSchemaNotReady(
                    "Checkpoint 结构未初始化，请运行 scripts/init_checkpoints.py --apply"
                )
            await cursor.execute("SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1")
            row = await cursor.fetchone()
            expected = len(AsyncPostgresSaver.MIGRATIONS) - 1
            if not row or row["v"] != expected:
                found = row["v"] if row else None
                raise CheckpointSchemaNotReady(
                    f"Checkpoint 迁移版本不匹配: 数据库 {found}，期望 {expected}；"
                    "请运行 scripts/init_checkpoints.py --apply"
                )
        yield AsyncPostgresSaver(conn)
    finally:
        await conn.close()
