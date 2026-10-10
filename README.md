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
- 配置基础文件为 `conf/config.yml`（复制 `conf/config.yml.example` 填写，含密钥不入库）；
  5174 配置后台将非密钥修改写入忽略文件 `conf/config.ui.yml`，重启 AI 服务后生效。
  密钥仍由基础文件或环境变量注入，不传到浏览器。可用 `SAI_CONFIG` 指定其他基础配置路径，
  但非本仓 `conf/config.yml` 时后台只读拒绝保存。
- 独立配置后台 `frontend/`（Svelte 5 + Tailwind v4 + shadcn-svelte）：管理模型、CRM、ASR/OCR/OSS
  和企业查询 MCP 的非密钥参数（配置全部入库）。产物为静态 SPA，**已随 Docker 镜像发布**，
  由 API 同端口挂在 `/admin`（容器访问 `http://<host>:8310/admin`），无需单独部署前端。
  开关：`admin.enabled`（总开关）+ `admin.allow_prod`（生产放行，默认关闭）；非生产环境还需
  `api.playground.enabled=true`。开发时 `npm run dev` 访问 `http://localhost:5173/admin/`。
   业务链路继续使用 `tests/h5/` 联调页。
 - H5 测试联调页（随镜像部署）：同一页面已打进镜像，由 API 同端口挂在 `/h5`
   （如 `https://<AI 域名>/h5`），与 `/playground` 同一开关：仅 dev/test 且
   `api.playground.enabled=true`，prod 一律 404。代码集中在 `app/devtools/h5`
   （**测试面，与业务代码隔离，清理时整目录删除**）；CRM 地址与登录凭据在配置后台
   「外部接口 → CRM 接口」填 `base_url` 与凭据 `client_secret`（token 端点/client_id/scopes 有默认值）。
   本地开发仍可用 `scripts/h5_server.py` 起在 8320 端口。
- CI：`.github/workflows/ci.yml` —— 只做构建与推送，**仅在推送 `v*` 标签或手动触发时运行**（推 main / 提 PR 不触发）；
   多阶段 Docker 构建（前端编译 + Python 运行时）→ 推送 GHCR 与 Quay
   （`ghcr.io/<owner>/<repo>`、`quay.io/zrcrm/sales-crm-ai`：latest / sha / 语义版本 tag；
   Quay 需配置 `QUAY_USERNAME`/`QUAY_PASSWORD` secrets）。测试不在 CI 运行，本地执行：
   `.venv\Scripts\python -m pytest tests/ -v`（命令见下方「测试命令」）。
- 镜像发布：`scripts\dockerbuild.ps1`（或 bash 版 `scripts/dockerbuild.sh`）自动计算下一个 `v*` 标签
  并推送，由 CI 完成构建与 GHCR + Quay 双仓库发布。
- 代码同步 GitLab：`scripts\sync-gitlab.ps1`（bash 版 `scripts/sync-gitlab.sh`）双向同步私有镜像仓
  `gitlab.lunz.cn`：默认推送当前分支（可选 `-Tags` / `-All`），`-Pull` 从 GitLab 拉取远端分支并合并到本地；
  首次用 `-Url` 配置 remote，凭据走 Git 凭据管理器或 `GITLAB_USERNAME`/`GITLAB_PASSWORD` 环境变量（不落盘），
  主仓库仍是 GitHub。
- 测试命令：`.venv\Scripts\python -m pytest tests/ -v`（conftest 自动在 `.local/pg-test` 起临时
  PostgreSQL；找不到 initdb 时真实库测试显式 skip，不伪造通过）。
- 快速开始：复制 `conf/config.yml.example` 为 `conf/config.yml` → `scripts\init_db.py --apply` →
  `scripts\init_checkpoints.py --apply` → `scripts\dev.ps1 start`（默认同时启动前端，`-NoUi` 跳过）。
  浏览器打开 http://127.0.0.1:5174 （前端）。
- 一键启动：`scripts\dev-all.ps1 start`（四服务：AI API :8310 / CRM :8080 / AI前端 :5173 / CRM前端 :81）。
- P4 的 M-03 建档已完成真实端到端联调；活动/任务侧、响应丢失与撤权场景仍待验收。P5 的 ASR/OCR POC 已通过，输入到 LLM 和正式业务确认链仍待闭环；P6 尚未完成。
- Docker 镜像采用 S6 守护 API 进程，`/ready` 检查数据库与 worker；构建上下文只包含运行所需文件。CI 只负责构建推送镜像，Python 测试在本地跑（见「测试命令」）。运行与故障验证见[测试规范](docs/测试规范.md)。

## Windows 注意

psycopg 异步模式要求 SelectorEventLoop；服务必须用 `scripts/serve.py` 启动
（`python -m uvicorn app.api.main:app` 在 Windows 上不可用）。

本文与代码同步维护；未实现的能力不在此声称可用。


