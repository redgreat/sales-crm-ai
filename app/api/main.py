"""FastAPI 应用工厂：健康/就绪分离，启动只校验不改库，worker 后台受管。

lifespan 持有：psycopg 连接池、checkpoint saver（及其连接）、编译图、worker 任务。
checkpoint/迁移结构未就绪时服务可启动但 /ready 失败——不降级无状态图。
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.api import deps
from app.api.routes import (
    auth as auth_routes,
    capabilities,
    conversations,
    files,
    health,
    knowledge,
    playground,
    research,
    settings as settings_routes,
    runs,
)
from app.config import PROJECT_ROOT, Settings, get_settings
from app.errors import ApiError, NotFound
from app.graphs.builder import compile_graph_for, graph_registry_version
from app.integrations.crm import build_crm_client
from app.persistence.checkpoints import CheckpointSchemaNotReady, check_checkpoint_schema, open_postgres_saver
from app.persistence.migrations import check_schema_revision
from app.persistence.pool import open_pool
from app.providers.factory import build_chat_model
from app.runtime.executor import Executor, run_worker_loop, stop_workers

logger = logging.getLogger("sales-crm-ai")

CAPABILITIES = tuple(graph_registry_version().keys())

# 配置后台静态产物：镜像内打在 /app/admin-dist（Dockerfile 复制 frontend/build），
# 本地开发直接指向 frontend/build；可用 SAI_ADMIN_DIST 覆盖。
_ADMIN_DIST_CANDIDATES = (Path("admin-dist"), Path("frontend") / "build")


def _admin_dist_dir() -> Path | None:
    candidates: list[Path] = []
    override = os.environ.get("SAI_ADMIN_DIST")
    if override:
        candidates.append(Path(override))
    candidates.extend(PROJECT_ROOT / name for name in _ADMIN_DIST_CANDIDATES)
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _install_admin_ui(app: FastAPI) -> None:
    """把配置后台前端（静态 SPA）挂在 /admin，与 API 同端口提供。

    产物由 Dockerfile 构建阶段产出；未打包时只给一条明确提示，不影响 API。
    """
    dist = _admin_dist_dir()
    if dist is None:
        logger.warning("未找到后台静态产物（admin-dist / frontend/build），/admin 暂不可用")

        @app.get("/admin", include_in_schema=False)
        @app.get("/admin/{full_path:path}", include_in_schema=False)
        async def admin_ui_missing() -> JSONResponse:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "NOT_FOUND",
                                   "message": "后台前端未打包进镜像（缺少 admin-dist/index.html）"}},
            )

        return

    index = dist / "index.html"
    root = dist.resolve()

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{full_path:path}", include_in_schema=False)
    async def admin_ui(full_path: str = "") -> Response:
        target = (dist / full_path) if full_path else index
        try:
            resolved = target.resolve()
        except OSError:
            resolved = index
        # 目录穿越防护：只允许读取产物目录内的文件
        if not (resolved.is_file() and root in resolved.parents):
            resolved = index
        # 带 hash 的构建产物长缓存，入口 HTML 不缓存
        immutable = "_app/immutable" in str(resolved).replace("\\", "/")
        cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return Response(
            content=resolved.read_bytes(),
            media_type=_media_type(resolved),
            headers={"cache-control": cache},
        )

    logger.info("后台前端已挂载：/admin（产物目录 %s）", dist)


def _media_type(path: Path) -> str:
    import mimetypes

    guessed = mimetypes.guess_type(str(path))[0]
    if guessed:
        return guessed if not guessed.startswith("text/") else f"{guessed}; charset=utf-8"
    return "application/octet-stream"


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

            # 后台配置（库）覆盖 config.yml：改动在后台保存后立即生效
            from app.admin.effective import bootstrap_admin_account, load_effective_settings

            # 生效配置：config.yml 打底，库里启用的连接覆盖其接管段落
            active = await load_effective_settings(pool, settings)
            app.state.settings = active
            await bootstrap_admin_account(pool, active)

            # 只读校验（不执行任何 DDL）
            async with pool.connection() as conn:
                schema_ok, revision = await check_schema_revision(conn)
            checkpoint_ok = False
            checkpoint_version: int | str | None = None
            try:
                checkpoint_ok, checkpoint_version = await check_checkpoint_schema(active.database_url)
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

            model = build_chat_model(active)  # prod + stub 在此直接失败
            app.state.model = model
            crm_client = build_crm_client(active)

            graphs: dict[str, Any] = {}
            app.state.graphs = graphs
            saver_cm = (
                open_postgres_saver(active.database_url)
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
                        lease_seconds=active.worker.lease_seconds,
                        max_attempts_default=active.worker.max_attempts,
                        backoff_base_seconds=active.worker.backoff_base_seconds,
                        batch_size=1,
                        conversations_ttl_hours=active.conversations.ttl_hours,
                        settings=active,
                        crm_client=crm_client,
                        model=model,
                    )
                    stop_event = asyncio.Event()
                    app.state.stop_event = stop_event
                    workers: list[asyncio.Task[Any]] = []
                    if active.worker.enabled and schema_ok:
                        for index in range(active.worker.concurrency):
                            workers.append(
                                asyncio.create_task(
                                    run_worker_loop(
                                        app.state.executor,
                                        poll_interval_seconds=active.worker.poll_interval_seconds,
                                        reap_interval_seconds=active.worker.reap_interval_seconds,
                                        stop_event=stop_event,
                                    ),
                                    name=f"ai-worker-{index}",
                                )
                            )
                    app.state.workers = workers
                    try:
                        yield
                    finally:
                        await stop_workers(
                            workers,
                            stop_event,
                            timeout_seconds=active.worker.shutdown_grace_seconds,
                        )
                        if crm_client is not None and hasattr(crm_client, "aclose"):
                            await crm_client.aclose()
            else:
                try:
                    yield
                finally:
                    if crm_client is not None and hasattr(crm_client, "aclose"):
                        await crm_client.aclose()

    app = FastAPI(title="sales-crm-ai", version="0.1.0", lifespan=lifespan)
    _install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(runs.router, dependencies=[_not_for_health()])
    app.include_router(conversations.router, dependencies=[_not_for_health()])
    # 知识授权索引同步（需求 4.16）：CRM 发布/停用/撤权时调用
    app.include_router(knowledge.router, dependencies=[_not_for_health()])
    # 能力目录（M-12）：前端入口契约的唯一真源
    app.include_router(capabilities.router, dependencies=[_not_for_health()])
    # 普通文件解析（需求 4.14）：纯文本解析，不落库、不调模型
    app.include_router(files.router, dependencies=[_not_for_health()])
    app.include_router(research.router, dependencies=[_not_for_health()])
    # 管理端接口：浏览器直连（同源 /api/v1），鉴权只认管理端会话 Token 与权限点，
    # 不再叠加服务间签名——否则后台无法随镜像部署到生产（代理只开在 dev/test）。
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(playground.router)
    # H5 测试联调页（app/devtools/h5，整目录可删；与 /playground 同一开关）
    from app.devtools.h5 import router as h5_router

    app.include_router(h5_router)
    # 配置后台前端（静态 SPA，与 API 同端口）
    _install_admin_ui(app)
    return app


def _not_for_health() -> Any:
    from fastapi import Depends

    return Depends(deps.require_operator)


def app_factory() -> FastAPI:  # 兼容命名
    return create_app()


# uvicorn 入口: app.api.main:app（配置来自 conf/config.yml 或 SAI_CONFIG 指定路径）
app = create_app(get_settings())
