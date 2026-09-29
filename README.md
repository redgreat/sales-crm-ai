# sales-crm-ai
基于 AgentZR 模板选择性重建的 Python CRM AI 服务。

- [智能体实施入口](AGENTS.md)
- [AI 接入需求](docs/AI接入需求文档.md)
- [AI 实施计划](docs/AI接入实施计划.md)
- [模板复用清单（P0）](docs/模板复用清单.md)
- [交接说明](docs/交接说明.md)

目标：FastAPI + LangGraph + PostgreSQL checkpoint，去掉 AgentZR 网关及计费功能，与现有 CRM 后端点对点集成。联调前端基于 SvelteKit + Tailwind + shadcn-svelte（lyra 风格：暗黑主题、无圆角、企业风）。

## 当前进度（2026-09-29）

- P0/P1/P2/P3 核心已实现并通过真实 PostgreSQL 测试：单轮抽取图（Stub 模型）、持久 checkpoint、
  Run 队列/租约/执行代次、interrupt/resume 多轮补参、会话绑定、签名认证与防重放、usage 剥离。
- 配置统一放 `conf/config.yml`（复制 `conf/config.yml.example` 填写，含密钥不入库）；
  可用 `SAI_CONFIG` 指定其他路径。不用环境变量文件。
- 联调前端 `frontend/`（Svelte 5 + Tailwind v4 + shadcn-svelte）：沟通抽取、会话多轮补参、
  Run 查询；未实现能力明确标记。浏览器不持密钥——前端经 `/playground/api` 开发代理访问，
  服务端注入固定联调身份并签名；仅 dev/test 且 `api.playground.enabled` 时生效，生产 404 且拒绝启动。
- CI：`.github/workflows/ci.yml` —— 前端编译 + Docker 镜像打包推送 GHCR
  （`ghcr.io/<owner>/<repo>:latest` / sha / 语义版本 tag）；CI 不跑测试，测试本地执行。
- 测试命令：`.venv\Scripts\python -m pytest tests/ -v`（conftest 自动在 `.local/pg-test` 起临时
  PostgreSQL；找不到 initdb 时真实库测试显式 skip，不伪造通过）。
- 快速开始：复制 `conf/config.yml.example` 为 `conf/config.yml` → `scripts\init_db.py --apply` →
  `scripts\init_checkpoints.py --apply` → `scripts\dev.ps1 start`（默认同时启动前端，`-NoUi` 跳过）。
  浏览器打开 http://127.0.0.1:5174 （前端）。
- 仍未完成：P4 CRM 真实闭环（需 CRM Java 薄层）、P5 全业务能力、P6 验收；
  真实模型 Provider 未实测（缺凭据，Stub 与真实 Provider 边界已固化并在生产拒绝 Stub）。

## Windows 注意

psycopg 异步模式要求 SelectorEventLoop；服务必须用 `scripts/serve.py` 启动
（`python -m uvicorn app.api.main:app` 在 Windows 上不可用）。

本文与代码同步维护；未实现的能力不在此声称可用。


