"""统一配置：conf/config.yml（YAML），不用环境变量文件。

查找顺序：显式路径 > SAI_CONFIG 环境变量（仅指路径，非密钥）> ./conf/config.yml >
项目根 conf/config.yml；都缺失时用默认值（测试/冒烟可直接构造）。

密钥有三个来源，优先级从低到高：config.yml 明文 < secrets.ui.yml（后台写入）
< 环境变量（见 _SECRET_ENV_OVERRIDES，推荐用于部署注入）。
密钥一律不进日志与代码。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from app.net_guard import OutboundUrlRejected, assert_public_https_url

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


class ResearchMcpProvider(BaseModel):
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    tool_name: str = ""
    query_argument: str = "query"
    timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    # 出站主机白名单（精确主机或父域）。留空表示不额外限制，但私网/回环地址始终拒绝。
    # 该字段不开放给配置后台编辑，避免管理员自行放行任意地址。
    allowed_hosts: list[str] = Field(default_factory=list)


class ResearchSettings(BaseModel):
    bocha: ResearchMcpProvider = Field(default_factory=ResearchMcpProvider)
    qichacha: ResearchMcpProvider = Field(default_factory=ResearchMcpProvider)

    @model_validator(mode="after")
    def _validate_outbound_urls(self) -> "ResearchSettings":
        """启动即拒绝明显危险的出站地址（不做 DNS，避免启动依赖外部解析）。

        真正的解析级校验（域名指向私网）在每次出站前执行，见 app/net_guard.py。
        """
        for name, provider in (("bocha", self.bocha), ("qichacha", self.qichacha)):
            if not provider.enabled or not provider.url:
                continue
            try:
                assert_public_https_url(
                    provider.url, allowed_hosts=provider.allowed_hosts, resolve=False
                )
            except OutboundUrlRejected as exc:
                raise SettingsError(f"research.{name}.url 不允许出站: {exc}") from None
        return self


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


class AdminSettings(BaseModel):
    """配置后台（/admin 页面与其后台 API）的开关。

    - `enabled`：总开关。关闭时页面与后台 API 一律 404。
    - `allow_prod`：生产环境放行开关。生产默认不暴露管理面；
      确需在生产维护配置时显式置 true，并只限内网访问。
    """

    enabled: bool = True
    allow_prod: bool = False


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
    "research.bocha.api_key": "SAI_BOCHA_MCP_API_KEY",
    "research.qichacha.api_key": "SAI_QICHACHA_MCP_API_KEY",
}

# 布尔开关的环境变量覆盖表（部署侧可不改 config.yml 就开关后台）。
_BOOL_ENV_OVERRIDES: dict[str, str] = {
    "admin.enabled": "SAI_ADMIN_ENABLED",
    "admin.allow_prod": "SAI_ADMIN_ALLOW_PROD",
}

_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


def _parse_bool_env(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    return None


class Settings(BaseModel):
    environment: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:55432/sales_crm_ai"
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_statement_timeout_ms: int = 30_000

    model: ModelSettings = Field(default_factory=ModelSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    crm: CrmSettings = Field(default_factory=CrmSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    # 增强输入（P5 需求 4.12/4.13）：默认关闭，凭据齐备后显式开启
    asr: AsrSettings = Field(default_factory=AsrSettings)
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    oss: OssSettings = Field(default_factory=OssSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    conversations: ConversationsSettings = Field(default_factory=ConversationsSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)

    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _apply_env_overrides(cls, data: Any) -> Any:
        """用环境变量覆盖密钥类配置（环境变量优先于配置文件）。"""
        if not isinstance(data, dict):
            return data
        for table, parse in ((_SECRET_ENV_OVERRIDES, None), (_BOOL_ENV_OVERRIDES, _parse_bool_env)):
            for dotted, env_name in table.items():
                raw = os.environ.get(env_name)
                if not raw:
                    continue
                value: Any = raw if parse is None else parse(raw)
                if value is None:
                    continue
                target = data
                parts = dotted.split(".")
                for part in parts[:-1]:
                    child = target.get(part)
                    if not isinstance(child, dict):
                        child = {}
                        target[part] = child
                    target = child
                target[parts[-1]] = value
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
        for name, provider in (("bocha", self.research.bocha), ("qichacha", self.research.qichacha)):
            if provider.enabled:
                if not provider.url.startswith("https://") or not provider.api_key or not provider.tool_name:
                    raise SettingsError(f"research.{name} 启用时必须配置 HTTPS url、api_key 和 tool_name")
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        """从 YAML 加载；找不到文件时返回默认值（不报错，供测试/冒烟）。"""
        for candidate in _candidates(path):
            return cls(**read_config_data(candidate))
        return cls()

    @classmethod
    def load_existing(cls, path: str | Path | None = None) -> tuple["Settings", Path | None]:
        """加载并返回实际使用的配置文件（预检用：缺失时显式报告）。"""
        for candidate in _candidates(path):
            return cls(**read_config_data(candidate)), candidate
        return cls(), None


def overlay_path(config_path: Path) -> Path:
    return config_path.with_name("config.ui.yml")


def read_config_data(config_path: Path) -> dict[str, Any]:
    """基础配置 → 后台非密钥覆盖 → 后台密钥 → 启用连接映射。

    顺序即优先级：后者覆盖前者。连接列表最后执行，保证「列表里启用的那条」
    才是运行期真相。环境变量覆盖发生在 Settings 的 before 校验器里，
    仍高于所有文件层。
    """
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SettingsError("config.yml 顶层必须是对象")
    return data


def _load_layer(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    values = yaml.safe_load(raw) or {}
    if not isinstance(values, dict):
        raise SettingsError(f"{label} 顶层必须是对象")
    return values


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
