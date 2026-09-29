"""迁移与 checkpoint 结构测试（真实 PostgreSQL）。"""
from __future__ import annotations

import psycopg

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.persistence import migrations
from app.persistence.checkpoints import check_checkpoint_schema, open_postgres_saver

pytestmark = pytest.mark.realpg


async def test_migrations_apply_and_check(db_url: str):
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        ok, current = await migrations.check_schema_revision(conn)
        assert ok
        assert current == migrations.required_revision()

        # 幂等：重复 apply 不报错、不重复
        applied = await migrations.apply_migrations(conn)
        assert applied == []


async def test_checkpoint_schema_ready(db_url: str):
    ok, version = await check_checkpoint_schema(db_url)
    assert ok
    assert version == len(AsyncPostgresSaver.MIGRATIONS) - 1


async def test_open_saver_rejects_uninitialized(db_url: str):
    """checkpoint 结构缺失时拒绝打开（不降级无状态）。"""
    base = db_url.rsplit("/", 1)[0]
    uninit_url = f"{base}/t_uninit_check"
    admin_url = f"{base}/postgres"
    async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as conn:
        await conn.execute("DROP DATABASE IF EXISTS t_uninit_check")
        await conn.execute("CREATE DATABASE t_uninit_check")
    try:
        with pytest.raises(Exception):
            async with open_postgres_saver(uninit_url):
                pass
    finally:
        async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as conn:
            await conn.execute("DROP DATABASE IF EXISTS t_uninit_check")
