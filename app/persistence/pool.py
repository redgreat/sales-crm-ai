"""单一 psycopg 异步连接池；在应用 lifespan 中创建并持有，避免每请求建引擎。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg_pool import AsyncConnectionPool

from app.config import Settings


def create_pool(settings: Settings) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        open=False,
        kwargs={"autocommit": True},
    )
    return pool


@asynccontextmanager
async def open_pool(settings: Settings) -> AsyncIterator[AsyncConnectionPool]:
    pool = create_pool(settings)
    try:
        await pool.open(wait=True, timeout=15)
        yield pool
    finally:
        await pool.close()


@asynccontextmanager
async def connect(settings_or_dsn: "Settings | str") -> AsyncIterator[psycopg.AsyncConnection]:
    """一次性连接（脚本/迁移用），运行期请用池。"""
    dsn = (
        settings_or_dsn.database_url
        if isinstance(settings_or_dsn, Settings)
        else settings_or_dsn
    )
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()
