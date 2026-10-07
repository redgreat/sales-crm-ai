"""P2 崩溃恢复测试使用的隔离 worker 子进程。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import Settings
from app.graphs.builder import compile_graph_for
from app.persistence import runs as runs_repo
from app.persistence.checkpoints import open_postgres_saver
from app.persistence.pool import open_pool
from app.providers.stub import StubChatModel
from app.runtime.executor import Executor


async def main() -> None:
    settings = Settings(
        environment="test",
        database_url=os.environ["SAI_P2_TEST_DSN"],
        model={"provider": "stub"},
    )
    async with open_pool(settings) as pool:
        if sys.argv[1] == "claim":
            claimed = await runs_repo.claim_next_runs(
                pool, worker_id="crash-test-worker", limit=1, lease_seconds=1
            )
            if len(claimed) != 1:
                raise RuntimeError(f"Expected one Run, got {len(claimed)}")
            print("CLAIMED", flush=True)
            await asyncio.Event().wait()
        elif sys.argv[1] == "execute_hold":
            class HoldingStub(StubChatModel):
                async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                    print("MODEL_ENTERED", flush=True)
                    await asyncio.Event().wait()

            async with open_postgres_saver(settings.database_url) as saver:
                graph = compile_graph_for("communication.extract", HoldingStub(), checkpointer=saver)
                executor = Executor(pool, {"communication.extract": graph}, lease_seconds=1, batch_size=1)
                await executor.run_batch()
        elif sys.argv[1] == "recover":
            async with open_postgres_saver(settings.database_url) as saver:
                graph = compile_graph_for("communication.extract", StubChatModel(), checkpointer=saver)
                executor = Executor(pool, {"communication.extract": graph}, lease_seconds=1, batch_size=1)
                deadline = asyncio.get_running_loop().time() + 10
                while asyncio.get_running_loop().time() < deadline:
                    await executor.reap_expired_leases()
                    if await executor.run_batch():
                        print("RECOVERED", flush=True)
                        return
                    await asyncio.sleep(0.1)
                raise TimeoutError("Expired Run was not recovered")
        else:
            raise ValueError(sys.argv[1])


if __name__ == "__main__":
    asyncio.run(main())
