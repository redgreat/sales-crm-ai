"""FastAPI 应用工厂：健康/就绪分离，启动只校验不改库，worker 后台受管。

lifespan 持有：psycopg 连接池、checkpoint saver（及其连接）、编译图、worker 任务。
checkpoint/迁移结构未就绪时服务可启动但 /ready 失败——不降级无状态图。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import deps
from app.api.routes import conversations, health, knowledge, playground, runs
from app.config import Settings, get_settings
from app.errors import ApiError
from app.graphs.builder import compile_graph_for, graph_registry_version
from app.persistence.checkpoints import CheckpointSchemaNotReady, check_checkpoint_schema, open_postgres_saver
from app.persistence.migrations import check_schema_revision
from app.persistence.pool import open_pool
from app.providers.factory import build_chat_model
from app.runtime.executor import Executor, run_worker_loop

logger = logging.getLogger("sales-crm-ai")

CAPABILITIES = tuple(graph_registry_version().keys())


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        payload = ApiError("内部错误").to_payload()
        return JSONResponse(status_code=500, content=payload)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_pool(settings) as pool:
            app.state.pool = pool
            app.state.settings = settings

            # 只读校验（不执行任何 DDL）
            async with pool.connection() as conn:
                schema_ok, revision = await check_schema_revision(conn)
            checkpoint_ok = False
            checkpoint_version: int | str | None = None
            try:
                checkpoint_ok, checkpoint_version = await check_checkpoint_schema(settings.database_url)
            except CheckpointSchemaNotReady:
                checkpoint_ok = False
            app.state.schema_ok = schema_ok
            app.state.schema_revision = revision
            app.state.checkpoint_ok = checkpoint_ok
            app.state.checkpoint_version = checkpoint_version
            if not schema_ok:
                logger.error("业务表结构未就绪（当前 %s），请运行 scripts/init_db.py --apply", revision)
            if not checkpoint_ok:
                logger.error("checkpoint 结构未就绪，请运行 scripts/init_checkpoints.py --apply")

            model = build_chat_model(settings)  # prod + stub 在此直接失败
            app.state.model = model

            graphs: dict[str, Any] = {}
            app.state.graphs = graphs
            saver_cm = (
                open_postgres_saver(settings.database_url)
                if checkpoint_ok
                else None
            )
            # checkpoint 未就绪：不打开 saver（open_postgres_saver 会拒绝），
            # 服务仍可启动但 /ready 失败且不启动 worker——不降级无状态图。
            if saver_cm is not None:
                async with saver_cm as saver:
                    for capability in CAPABILITIES:
                        graphs[capability] = compile_graph_for(capability, model, checkpointer=saver)
                    app.state.executor = Executor(
                        pool,
                        graphs,
                        lease_seconds=settings.worker.lease_seconds,
                        max_attempts_default=settings.worker.max_attempts,
                        backoff_base_seconds=settings.worker.backoff_base_seconds,
                        batch_size=1,
                    )
                    stop_event = asyncio.Event()
                    app.state.stop_event = stop_event
                    workers: list[asyncio.Task[Any]] = []
                    if settings.worker.enabled and schema_ok:
                        for index in range(settings.worker.concurrency):
                            workers.append(
                                asyncio.create_task(
                                    run_worker_loop(
                                        app.state.executor,
                                        poll_interval_seconds=settings.worker.poll_interval_seconds,
                                        reap_interval_seconds=settings.worker.reap_interval_seconds,
                                        stop_event=stop_event,
                                    ),
                                    name=f"ai-worker-{index}",
                                )
                            )
                    app.state.workers = workers
                    try:
                        yield
                    finally:
                        stop_event.set()
                        if workers:
                            await asyncio.gather(*workers, return_exceptions=True)
            else:
                yield

    app = FastAPI(title="sales-crm-ai", version="0.1.0", lifespan=lifespan)
    _install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(runs.router, dependencies=[_not_for_health()])
    app.include_router(conversations.router, dependencies=[_not_for_health()])
    # 知识授权索引同步（需求 4.16）：CRM 发布/停用/撤权时调用
    app.include_router(knowledge.router, dependencies=[_not_for_health()])
    app.include_router(playground.router)
    return app


def _not_for_health() -> Any:
    from fastapi import Depends

    return Depends(deps.require_operator)


def app_factory() -> FastAPI:  # 兼容命名
    return create_app()


# uvicorn 入口: app.api.main:app（配置来自 conf/config.yml 或 SAI_CONFIG 指定路径）
app = create_app(get_settings())
