"""sales-crm-ai：CRM 专用 AI 服务。

FastAPI + LangGraph + PostgreSQL checkpoint，模型直连 Provider，
与 CRM Java 后端点对点集成；不含网关、平台 Key、计费。
"""

import asyncio
import sys

# psycopg 异步模式不兼容 Windows 默认的 ProactorEventLoop；
# 统一切换为 SelectorEventLoop（uvicorn 在 Windows 上也要求该策略）。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

APP_NAME = "sales-crm-ai"
