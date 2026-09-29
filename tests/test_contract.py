"""P1 契约测试：无网关服务/无网关表可启动；Stub 显式且生产拒绝；缺结构不降级。"""
from __future__ import annotations

import httpx
import psycopg
import pytest

from app.api.main import create_app
from app.config import ModelSettings, Settings, SettingsError
from app.providers.factory import ProviderConfigError, build_chat_model

pytestmark = pytest.mark.realpg


def test_stub_rejected_in_prod_settings():
    with pytest.raises(SettingsError):
        Settings(environment="prod", model={"provider": "stub"}, auth={"service_secret": "x"})


def test_prod_requires_service_secret():
    with pytest.raises(SettingsError):
        Settings(
            environment="prod",
            model={"provider": "openai_compatible", "base_url": "https://api.example.com/v1",
                   "api_key": "k", "name": "m"},
            auth={"service_secret": ""},
        )


def test_prod_rejects_playground():
    with pytest.raises(SettingsError):
        Settings(
            environment="prod",
            model={"provider": "openai_compatible", "base_url": "https://api.example.com/v1",
                   "api_key": "k", "name": "m"},
            auth={"service_secret": "x"},
            api={"playground": {"enabled": True}},
        )


def test_openai_compatible_requires_full_config():
    with pytest.raises(SettingsError):
        Settings(model={"provider": "openai_compatible", "api_key": "k"})


def test_stub_build_refuses_prod_even_if_bypassed():
    settings = Settings(
        environment="prod",
        model={"provider": "openai_compatible", "base_url": "https://api.example.com/v1",
               "api_key": "k", "name": "m"},
        auth={"service_secret": "x"},
    )
    settings.model = ModelSettings(provider="stub")
    with pytest.raises(ProviderConfigError):
        build_chat_model(settings)


def test_no_gateway_dependencies_in_source():
    """import 语句不引用任何网关/计费模块（docstring 提及来源仓库不算依赖）。"""
    banned = ("smart_gateway", "open_gateway", "open_platform",
              "log_llm_token_usage", "calculate_usage_cost", "agentzr")
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    for path in app_dir.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for token in banned:
                assert token not in stripped.lower(), (
                    f"{path.name} 引用了被排除的网关/计费符号: {stripped}"
                )


async def test_app_starts_but_not_ready_without_schema(pg_cluster):
    """无业务表/无 checkpoint 表：服务可启动（健康），/ready 失败，不降级无状态图。"""
    name = "t_unschema_contract"
    async with await psycopg.AsyncConnection.connect(
        pg_cluster.url("postgres"), autocommit=True
    ) as conn:
        await conn.execute(f"DROP DATABASE IF EXISTS {name}")
        await conn.execute(f'CREATE DATABASE "{name}"')
    try:
        settings = Settings(
            environment="test", database_url=pg_cluster.url(name),
            model={"provider": "stub"},
            worker={"enabled": False, "poll_interval_seconds": 0.05},
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://ai") as client:
                assert (await client.get("/health")).status_code == 200
                ready = await client.get("/ready")
                assert ready.status_code == 503
                checks = ready.json()["checks"]
                assert not checks["schema"]["ok"] and not checks["checkpoint"]["ok"]
    finally:
        async with await psycopg.AsyncConnection.connect(
            pg_cluster.url("postgres"), autocommit=True
        ) as conn:
            await conn.execute(f"DROP DATABASE IF EXISTS {name}")
