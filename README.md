# sales-crm-ai
基于 AgentZR 模板选择性重建的 Python CRM AI 服务。

- [智能体实施入口](AGENTS.md)
- [AI 接入需求](docs/AI接入需求文档.md)
- [AI 实施计划](docs/AI接入实施计划.md)

目标：FastAPI + LangGraph + PostgreSQL checkpoint，去掉 AgentZR 网关及计费功能，与现有 CRM 后端点对点集成。联调前端基于 SvelteKit + Tailwind + shadcn-svelte（lyra 风格：暗黑主题、无圆角、企业风）。

## 当前进度（2026-10-07）

- P0/P1/P2/P3/P4 核心已实现并通过真实 PostgreSQL 测试：单轮抽取图（Stub 模型）、持久 checkpoint、
  Run 队列/租约/执行代次、interrupt/resume 多轮补参、会话绑定、签名认证与防重放、usage 剥离。
- P4 CRM 集成薄层已实现：候选导入/确认写入/幂等/权限验证，真实模型（LongCat）实测通过。
- 配置统一放 `conf/config.yml`（复制 `conf/config.yml.example` 填写，含密钥不入库）；
  可用 `SAI_CONFIG` 指定其他路径。不用环境变量文件。
- 联调前端 `frontend/`（Svelte 5 + Tailwind v4 + shadcn-svelte）：沟通抽取、会话多轮补参、
  Run 查询；未实现能力明确标记。浏览器不持密钥——前端经 `/playground/api` 开发代理访问，
  服务端注入固定联调身份并签名；仅 dev/test 且 `api.playground.enabled` 时生效，生产 404 且拒绝启动。
- CI：`.github/workflows/ci.yml` —— 多阶段 Docker 构建（前端编译 + Python 运行时）→ 推送 GHCR 与 Quay
  （`ghcr.io/<owner>/<repo>`、`quay.io/zrcrm/sales-crm-ai`：latest / sha / 语义版本 tag；
  Quay 需配置 `QUAY_USERNAME`/`QUAY_PASSWORD` secrets）。
- 测试命令：`.venv\Scripts\python -m pytest tests/ -v`（conftest 自动在 `.local/pg-test` 起临时
  PostgreSQL；找不到 initdb 时真实库测试显式 skip，不伪造通过）。
- 快速开始：复制 `conf/config.yml.example` 为 `conf/config.yml` → `scripts\init_db.py --apply` →
  `scripts\init_checkpoints.py --apply` → `scripts\dev.ps1 start`（默认同时启动前端，`-NoUi` 跳过）。
  浏览器打开 http://127.0.0.1:5174 （前端）。
- 一键启动：`scripts\dev-all.ps1 start`（四服务：AI API :8310 / CRM :8080 / AI前端 :5173 / CRM前端 :81）。
- P4 的 M-03 建档已完成真实端到端联调；活动/任务侧、响应丢失与撤权场景仍待验收。P5 的 ASR/OCR POC 已通过，输入到 LLM 和正式业务确认链仍待闭环；P6 尚未完成。
- Docker 镜像采用 S6 守护 API 进程，`/ready` 检查数据库与 worker；构建上下文只包含运行所需文件。CI 的 Python 测试通过后才推送镜像。运行与故障验证见[测试规范](docs/测试规范.md)。

## Windows 注意

psycopg 异步模式要求 SelectorEventLoop；服务必须用 `scripts/serve.py` 启动
（`python -m uvicorn app.api.main:app` 在 Windows 上不可用）。

本文与代码同步维护；未实现的能力不在此声称可用。


