"""统一配置：conf/config.yml（YAML），不用环境变量文件。

查找顺序：显式路径 > SAI_CONFIG 环境变量（仅指路径，非密钥）> ./conf/config.yml >
项目根 conf/config.yml；都缺失时用默认值（测试/冒烟可直接构造）。
密钥只在 config.yml 中，不进日志与代码；config.yml 本身不入库（只提交 .example）。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATHS = (Path("conf/config.yml"), PROJECT_ROOT / "conf" / "config.yml")


class SettingsError(RuntimeError):
    """配置不满足运行要求。"""


class ModelSettings(BaseModel):
    provider: Literal["stub", "openai_compatible"] = "stub"
    base_url: str = ""
    api_key: str = ""
    name: str = ""
    temperature: float = 0.2
    timeout_seconds: float = 60.0
    max_retries: int = 2


class AuthSettings(BaseModel):
    service_key_id: str = "crm-ai"
    service_secret: str = ""
    timestamp_window_seconds: int = 300
    nonce_ttl_seconds: int = 600


class CrmSettings(BaseModel):
    base_url: str = ""
    key_id: str = "ai-crm"
    secret: str = ""
    timeout_seconds: float = 10.0


class WorkerSettings(BaseModel):
    enabled: bool = True
    concurrency: int = 2
    poll_interval_seconds: float = 0.5
    lease_seconds: int = 120
    max_attempts: int = 3
    backoff_base_seconds: float = 2.0
    reap_interval_seconds: float = 5.0


class PlaygroundSettings(BaseModel):
    """开发联调代理与前端入口；生产一律关闭（路由 404）。"""

    enabled: bool = False
    operator_user_id: str = "dev-playground"
    operator_user_name: str = "开发联调"


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8310
    playground: PlaygroundSettings = Field(default_factory=PlaygroundSettings)


class Settings(BaseModel):
    environment: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:55432/sales_crm_ai"
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_statement_timeout_ms: int = 30_000

    model: ModelSettings = Field(default_factory=ModelSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    crm: CrmSettings = Field(default_factory=CrmSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_environment(self) -> "Settings":
        if self.environment == "prod":
            if self.model.provider == "stub":
                raise SettingsError("生产环境禁止 Stub Provider（显式启用且生产拒绝）")
            if not self.auth.service_secret:
                raise SettingsError("生产环境必须配置 auth.service_secret")
            if self.api.playground.enabled:
                raise SettingsError("生产环境禁止开启 playground 联调代理")
        if self.model.provider == "openai_compatible":
            missing = [
                name
                for name, value in (
                    ("model.base_url", self.model.base_url),
                    ("model.api_key", self.model.api_key),
                    ("model.name", self.model.name),
                )
                if not value
            ]
            if missing:
                raise SettingsError(f"openai_compatible Provider 缺少配置: {', '.join(missing)}")
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        """从 YAML 加载；找不到文件时返回默认值（不报错，供测试/冒烟）。"""
        for candidate in _candidates(path):
            data: dict[str, Any] = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            return cls(**data)
        return cls()

    @classmethod
    def load_existing(cls, path: str | Path | None = None) -> tuple["Settings", Path | None]:
        """加载并返回实际使用的配置文件（预检用：缺失时显式报告）。"""
        for candidate in _candidates(path):
            data: dict[str, Any] = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            return cls(**data), candidate
        return cls(), None


def _candidates(path: str | Path | None) -> list[Path]:
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    env_path = os.environ.get("SAI_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(DEFAULT_CONFIG_PATHS)
    return [c for c in candidates if c.is_file()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()
