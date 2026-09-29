"""模型工厂：按配置直连 Provider。

- openai_compatible: ChatOpenAI（base_url 指向供应商或自建兼容端点），出站直连；
- stub: 显式启用，生产环境拒绝。
不查询数据库默认模型配置，不解析网关，不注册 usage/cost 回调。
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from app.config import Settings
from app.providers.stub import StubChatModel


class ProviderConfigError(RuntimeError):
    pass


def build_chat_model(settings: Settings) -> BaseChatModel:
    if settings.model.provider == "stub":
        if settings.environment == "prod":
            raise ProviderConfigError("生产环境禁止 Stub Provider")
        return StubChatModel()
    if settings.model.provider == "openai_compatible":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.model.name,
            base_url=settings.model.base_url or None,
            api_key=settings.model.api_key,
            temperature=settings.model.temperature,
            timeout=settings.model.timeout_seconds,
            max_retries=settings.model.max_retries,
        )
    raise ProviderConfigError(f"未知 model_provider: {settings.model.provider}")
