"""nonce 防重放：持久化存储，重复 nonce 拒绝，过期清理顺带完成。"""
from __future__ import annotations

import psycopg

from app.util import utcnow


async def register_nonce(
    pool: psycopg.AsyncConnectionPool, *, nonce: str, key_id: str, ttl_seconds: int
) -> bool:
    """登记 nonce。返回 False 表示重放（已存在）。

    过期 nonce 在同语句中清理，不依赖外部定时任务。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM auth_nonces WHERE expires_at < %s", (utcnow(),))
                await cur.execute(
                    """
                    INSERT INTO auth_nonces (nonce, key_id, expires_at)
                    VALUES (%s, %s, now() + make_interval(secs => %s))
                    ON CONFLICT (nonce) DO NOTHING
                    """,
                    (nonce, key_id, float(ttl_seconds)),
                )
                return cur.rowcount == 1
