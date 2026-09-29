"""测试基建。

真实 PostgreSQL：优先 SAI_TEST_DATABASE_URL；否则在 Windows/本机用
pg_ctl initdb 起临时实例（数据目录 .local/pg-test，gitignore 内）。
每个测试独立数据库（业务迁移 + checkpoint setup），互不污染。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_STATE_BIN = Path(r"C:\Users\wangcw\AppData\Local\activestate\cache\bin")


def _find_pg_bin() -> Path | None:
    env_dir = os.environ.get("SAI_PG_BIN")
    candidates = [Path(env_dir)] if env_dir else []
    which = shutil.which("initdb")
    if which:
        candidates.append(Path(which).parent)
    candidates.append(ACTIVE_STATE_BIN)
    for candidate in candidates:
        if (candidate / "initdb.exe").exists() or (candidate / "initdb").exists():
            return candidate
    return None


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PgCluster:
    """临时 PostgreSQL 实例句柄：支持启停（供重启恢复测试）。"""

    def __init__(self, bin_dir: Path, datadir: Path, port: int):
        self.bin_dir = bin_dir
        self.datadir = datadir
        self.port = port
        self.logfile = datadir / "pg.log"
        self.exe = lambda name: str(bin_dir / (name + (".exe" if os.name == "nt" else "")))

    @classmethod
    def provision(cls, bin_dir: Path, datadir: Path) -> "PgCluster":
        # 清理上次异常退出残留：先停掉占用该目录的孤儿实例，再清空目录
        if datadir.exists():
            _run([
                str(bin_dir / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")),
                "-D", str(datadir), "-m", "immediate", "stop",
            ])
            shutil.rmtree(datadir, ignore_errors=True)
        datadir.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        # --no-locale：中文 Windows locale（936）缺 text search 配置会失败
        result = _run([
            str(bin_dir / ("initdb.exe" if os.name == "nt" else "initdb")),
            "-D", str(datadir), "-U", "postgres", "-A", "trust",
            "--no-locale", "-E", "UTF8",
        ])
        if result.returncode != 0:
            raise RuntimeError(f"initdb 失败: {result.stderr}")
        cluster = cls(bin_dir, datadir, port)
        cluster.start()
        return cluster

    def start(self) -> None:
        # 不用 PIPE 捕获：pg_ctl start 的子进程 postgres 会继承管道写端，
        # capture_output 将永远等不到 EOF（Windows 经典坑）；改为落日志文件。
        with open(self.logfile, "ab") as logf:
            result = subprocess.run(
                [
                    self.exe("pg_ctl"), "-D", str(self.datadir),
                    "-o", f"-p {self.port} -h 127.0.0.1", "start",
                ],
                stdout=logf, stderr=logf,
            )
        if result.returncode != 0:
            raise RuntimeError(f"pg_ctl start 失败（见 {self.logfile}）")
        self._wait_ready()

    def stop(self, mode: str = "fast") -> None:
        _run([self.exe("pg_ctl"), "-D", str(self.datadir), "-m", mode, "stop"])

    def restart(self, mode: str = "fast") -> None:
        self.stop(mode)
        self.start()

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = _run([
                self.exe("pg_isready"), "-h", "127.0.0.1", "-p", str(self.port), "-U", "postgres",
            ])
            if result.returncode == 0:
                return
            time.sleep(0.3)
        raise RuntimeError("PostgreSQL 临时实例未就绪")

    def url(self, database: str) -> str:
        return f"postgresql://postgres@127.0.0.1:{self.port}/{database}"

    def create_database(self, name: str) -> None:
        with psycopg.connect(self.url("postgres"), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')

    def drop_database(self, name: str) -> None:
        with psycopg.connect(self.url("postgres"), autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="session")
def pg_cluster() -> PgCluster:
    """真实 PostgreSQL 实例；不可用时显式 skip（不伪造测试通过）。"""
    url_env = os.environ.get("SAI_TEST_DATABASE_URL")
    if url_env:
        pytest.skip("SAI_TEST_DATABASE_URL 模式暂不支持启停测试；请使用内置临时实例")
    bin_dir = _find_pg_bin()
    if bin_dir is None:
        pytest.skip("找不到 PostgreSQL 二进制（initdb/pg_ctl），跳过真实数据库测试")
    datadir = PROJECT_ROOT / ".local" / "pg-test"
    datadir.parent.mkdir(parents=True, exist_ok=True)
    cluster = PgCluster.provision(bin_dir, datadir)
    yield cluster
    cluster.stop(mode="immediate")


@pytest_asyncio.fixture
async def db_url(pg_cluster: PgCluster) -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    pg_cluster.create_database(name)
    url = pg_cluster.url(name)

    from app.persistence import migrations
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await migrations.apply_migrations(conn)
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
    yield url
    pg_cluster.drop_database(name)


@pytest_asyncio.fixture
async def db_pool(db_url: str):
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(conninfo=db_url, min_size=1, max_size=5, open=False,
                               kwargs={"autocommit": True})
    await pool.open(wait=True, timeout=15)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def graphs(db_url: str):
    """挂真实 PostgreSQL checkpoint 的编译图（Stub 模型）。"""
    from app.graphs.builder import compile_graph
    from app.persistence.checkpoints import open_postgres_saver
    from app.providers.stub import StubChatModel

    async with open_postgres_saver(db_url) as saver:
        yield {"communication.extract": compile_graph(StubChatModel(), checkpointer=saver)}


@pytest.fixture
def test_settings(db_url: str):
    from app.config import Settings

    return Settings(
        environment="test",
        database_url=db_url,
        model={"provider": "stub"},
        worker={
            "enabled": True,
            "concurrency": 2,
            "poll_interval_seconds": 0.05,
            "lease_seconds": 120,
            "backoff_base_seconds": 0.2,
        },
    )
