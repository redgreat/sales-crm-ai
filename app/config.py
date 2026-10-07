"""统一配置：conf/config.yml（YAML），不用环境变量文件。

查找顺序：显式路径 > SAI_CONFIG 环境变量（仅指路径，非密钥）> ./conf/config.yml >
项目根 conf/config.yml；都缺失时用默认值（测试/冒烟可直接构造）。

密钥有两个来源，互不冲突：config.yml（本身不入库，只提交 .example），
或环境变量（见 _SECRET_ENV_OVERRIDES，优先级更高，推荐用于部署注入）。
密钥一律不进日志与代码。
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


class AsrSettings(BaseModel):
    """录音文件识别（需求 4.12）——阿里云百炼非实时语音识别。

    硬约束：百炼只接受**公网可访问的 URL**，不支持 Base64/二进制/本地文件，
    因此本地音频通常需先入对象存储（见 OssSettings）取得 URL 后再提交。
    enabled=false 时能力明确不可用，绝不回退为"假装识别成功"。
    """

    enabled: bool = False
    provider: Literal["dashscope"] = "dashscope"
    api_key: str = ""
    # 百炼业务空间专属域名前缀；留空则使用旧域名 dashscope.aliyuncs.com
    workspace_id: str = ""
    region: str = "cn-beijing"
    model: str = "paraformer-v2"
    language_hints: list[str] = Field(default_factory=lambda: ["zh"])
    # 说话人分离：仅单声道、建议音频不超过 2 小时；只产出 speaker_id，不映射员工
    diarization_enabled: bool = True
    timeout_seconds: float = 30.0
    poll_interval_seconds: float = 3.0
    poll_timeout_seconds: float = 900.0

    def endpoint(self) -> str:
        if self.workspace_id:
            return f"https://{self.workspace_id}.{self.region}.maas.aliyuncs.com"
        return "https://dashscope.aliyuncs.com"


class OcrSettings(BaseModel):
    """图片文字识别（需求 4.13）——阿里云文字识别 OCR。

    凭据为 AccessKey（ID+Secret），不是 API Key；Endpoint 固定华东1（杭州）。
    图片可用 Url 或二进制 body 传入，故不强依赖对象存储。
    """

    enabled: bool = False
    access_key_id: str = ""
    access_key_secret: str = ""
    endpoint: str = "ocr-api.cn-hangzhou.aliyuncs.com"
    # RecognizeAllText 的 Type：Advanced=通用文字识别高精版
    type: str = "Advanced"
    # 原图锚点：points（多边形）或 rectangle（矩形）；空串表示不返回坐标
    output_coordinate: str = "points"
    timeout_seconds: float = 30.0


class OssSettings(BaseModel):
    """对象存储：为 ASR 提供公网可访问的音频 URL（ASR 不接受本地文件）。

    未启用时，ASR 只能接受调用方自行提供的公网 url；
    启用后才支持"本地文件 → 上传 → 签名 URL → 识别"的链路。
    """

    enabled: bool = False
    endpoint: str = ""
    bucket: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    signed_url_ttl_seconds: int = 3600


class WorkerSettings(BaseModel):
    enabled: bool = True
    concurrency: int = Field(default=2, gt=0)
    poll_interval_seconds: float = Field(default=0.5, gt=0)
    lease_seconds: int = Field(default=120, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    backoff_base_seconds: float = Field(default=2.0, ge=0)
    reap_interval_seconds: float = Field(default=5.0, gt=0)
    shutdown_grace_seconds: float = Field(default=5.0, gt=0, le=60)


class ConversationsSettings(BaseModel):
    """会话生命周期（P3 收口）：TTL 过期与历史窗口默认值。

    - ttl_hours：最后活动后超过该时长标记 expired（不能续问）。M-01 红线：
      会话过期只影响恢复语义，绝不改变候选业务状态。0 = 禁用过期清理。
    - history_window：历史装配/消息列表的默认最近窗口条数。
    """

    ttl_hours: int = 72
    history_window: int = 100


class PlaygroundSettings(BaseModel):
    """开发联调代理与前端入口；生产一律关闭（路由 404）。"""

    enabled: bool = False
    operator_user_id: str = "dev-playground"
    operator_user_name: str = "开发联调"


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8310
    playground: PlaygroundSettings = Field(default_factory=PlaygroundSettings)


# 密钥的环境变量覆盖表：「配置路径 -> 环境变量名」。
# 存在的意义：密钥可以完全不落盘（config.yml 虽已 gitignore，但环境变量更安全，
# 也更贴近部署侧的密钥注入方式）。环境变量优先级**高于**配置文件：
# 文件里留占位符、启动时由环境注入真实值，是推荐用法。
_SECRET_ENV_OVERRIDES: dict[str, str] = {
    "model.api_key": "SAI_MODEL_API_KEY",
    "crm.secret": "SAI_CRM_SECRET",
    "asr.api_key": "SAI_ASR_API_KEY",
    "asr.workspace_id": "SAI_ASR_WORKSPACE_ID",
    "ocr.access_key_id": "SAI_OCR_ACCESS_KEY_ID",
    "ocr.access_key_secret": "SAI_OCR_ACCESS_KEY_SECRET",
    "oss.access_key_id": "SAI_OSS_ACCESS_KEY_ID",
    "oss.access_key_secret": "SAI_OSS_ACCESS_KEY_SECRET",
    "oss.endpoint": "SAI_OSS_ENDPOINT",
    "oss.bucket": "SAI_OSS_BUCKET",
}


class Settings(BaseModel):
    environment: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:55432/sales_crm_ai"
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_statement_timeout_ms: int = 30_000

    model: ModelSettings = Field(default_factory=ModelSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    crm: CrmSettings = Field(default_factory=CrmSettings)
    # 增强输入（P5 需求 4.12/4.13）：默认关闭，凭据齐备后显式开启
    asr: AsrSettings = Field(default_factory=AsrSettings)
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    oss: OssSettings = Field(default_factory=OssSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    conversations: ConversationsSettings = Field(default_factory=ConversationsSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _apply_env_overrides(cls, data: Any) -> Any:
        """用环境变量覆盖密钥类配置（环境变量优先于配置文件）。"""
        if not isinstance(data, dict):
            return data
        for dotted, env_name in _SECRET_ENV_OVERRIDES.items():
            value = os.environ.get(env_name)
            if not value:
                continue
            section, field = dotted.split(".", 1)
            current = data.get(section)
            if isinstance(current, dict):
                current[field] = value
            else:
                data[section] = {field: value}
        return data

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
        # 增强输入：显式启用时必须凭据齐备；缺凭据直接拒绝启动，不回退为"假装可用"
        if self.asr.enabled and not self.asr.api_key:
            raise SettingsError("asr.enabled=true 时必须配置 asr.api_key（百炼 API Key）")
        if self.ocr.enabled:
            missing_ocr = [
                name
                for name, value in (
                    ("ocr.access_key_id", self.ocr.access_key_id),
                    ("ocr.access_key_secret", self.ocr.access_key_secret),
                )
                if not value
            ]
            if missing_ocr:
                raise SettingsError(f"ocr.enabled=true 时缺少配置: {', '.join(missing_ocr)}")
        if self.oss.enabled:
            missing_oss = [
                name
                for name, value in (
                    ("oss.endpoint", self.oss.endpoint),
                    ("oss.bucket", self.oss.bucket),
                    ("oss.access_key_id", self.oss.access_key_id),
                    ("oss.access_key_secret", self.oss.access_key_secret),
                )
                if not value
            ]
            if missing_oss:
                raise SettingsError(f"oss.enabled=true 时缺少配置: {', '.join(missing_oss)}")
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
