# sales-crm-ai 智能体实施入口

本项目采用 Python，以 AgentZR 为模板选择性重建 CRM AI 服务。先完整阅读 [需求](docs/AI接入需求文档.md) 和 [实施计划](docs/AI接入实施计划.md)，它们是新实施基线，旧 CRM Java AI 方案不再作为技术架构依据。

## 开发规范（写代码前必读，属强制约束）

| 规范 | 路径 | 何时读 |
|---|---|---|
| 前后端开发规范 | [docs/前后端开发规范.md](docs/前后端开发规范.md) | 动 Python 后端（§2）或前端（§3）代码前 |
| PostgreSQL 数据库规范 | [docs/PostgreSQL数据库规范.md](docs/PostgreSQL数据库规范.md) | 涉及 CRM 主库表/字段口径、写 SQL、对接 CRM 数据前 |
| 测试规范 | [docs/测试规范.md](docs/测试规范.md) | 联调、跑真实链路、用测试账号前 |

要点（详见各文件，不得绕开）：

- **分层单向依赖**：`app/api` → `app/runtime` → `app/graphs` → `app/integrations|knowledge|providers`，`app/persistence` 为底座；路由层不写业务逻辑与 SQL。
- **错误与配置**：业务错误只抛 `app/errors.py` 的 `ApiError` 子类；配置唯一入口 `app/config.py::Settings`，新增项同步 `conf/config.yml.example`。
- **密钥与用量**：密钥不入库不入日志；模型 usage/费用/配额不落日志、消息、checkpoint。
- **数据库**：AI 自有库 `zrcrm_ai` 变更走 `app/persistence/migrations/` 显式应用；**禁止直连 CRM 主库 `zrcrm`**，其表口径以 `docs/PostgreSQL数据库规范.md` 为准。
- **前端分工**：本仓 `frontend/` 是 SvelteKit 联调页（生产默认关闭、不持密钥）；正式业务前端是 `sales-crm-admin-ui` 的 Vue 2 + element-ui，不得引入 Vue 3 门户。
- **提交前**过 `docs/前后端开发规范.md` §5 清单；§6 为红线，一律禁止。

## 实施规则

- 先检查当前分支、git status 和已有实现，保留用户/其他 Agent 的改动；文档当前是计划，不代表功能已实现。
- 按 P0 → P1 → P2 可靠执行 → P3 最小多轮 → P4 CRM 闭环 → P5 全场景 → P6 验收推进；P-UI/P-DEV 按计划并行，不用页面演示代替闭环。
- 模板为 `D:/github/AgentZR`，只读参考，不修改它；选择性复用并记录源提交/许可证，不复制 .git、密钥、数据库内容、缓存或云部署配置。
- 新项目使用 FastAPI + LangGraph + PostgreSQL checkpoint，锁定实测依赖；不自研通用图引擎，不静默降级内存/无状态模式。
- 去掉 smart/open gateway、多通道、平台 Key 签发和 Token/费用/配额；清理模型运行层传递依赖，供应商 usage 不落日志/消息/checkpoint。服务认证与 CRM 权限不能删除。
- CRM Java 后端仍在 `D:/github/sales-crm-api-service`；只通过授权接口访问，正式活动/任务/日报/知识主档归 CRM，不直接访问其库。候选需本人确认后由 CRM 正式 Service 写入。
- CRM 前端为 `D:/github/sales-crm-admin-ui` 的 Vue2，不复制模板 Vue3 管理门户；测试页复用登录及权限，生产默认关闭。
- 所有新文档与联调脚本放本仓库；前端代码改在 CRM 前端仓库，CRM 集成薄层改在后端仓库，跨仓库改动先记录边界与版本。
- checkpoint 不代替执行租约、用户授权和业务幂等；验证重启恢复、旧执行者迟到、重复 resume、历史撤权、部分成功及跨库对账。
- 会话历史、窗口记忆和结构化补参分开；每轮重新鉴权，等待释放 worker，补参不是正式写入授权。
- 启动脚本遵循需求第 8 节：隐藏后台进程、预检依赖/端口、仅停止自己的进程；不自动迁移真实库或安装全局依赖。
- 每个工作包先写失败测试，再实现并交接测试命令/结果/阻塞/下一步。Stub、真实模型和正式 CRM 验收明确区分，缺凭据不能假称通过。
- 不擅自执行发布、远端分支删除、购买服务、外发真实业务数据或改生产环境；数据库迁移须显式授权，不在启动自动改库。

## MCP 服务

### GitHub MCP
- 配置位置：`~/AppData/Roaming/Xiaomi MiMo/engine-config/mimocode.json`
- 用途：查看 Actions 运行状态、管理 PR/Issue、读取仓库信息
- 认证：使用 Personal Access Token（已配置在 mimocode.json）
- 常用操作：`gh run list`、`gh run view`、`gh pr list`

### DB MCP（数据库）
- 连接 ID：**37**（本项目数据库，已配置读写权限）
- 用途：执行 SQL 查询、DDL 变更、数据迁移
- 可用工具：`dbmcp_execute_query`（只读）、`dbmcp_execute_sql`（DDL/DML）
- 注意：DDL 操作须显式授权，不在启动自动改库

## CI/CD

- 工作流：`.github/workflows/ci.yml`
- 触发：仅推送 `v*` 标签或 `workflow_dispatch`；推 main / 提 PR 不触发（测试不在 CI 运行，本地 `pytest tests/`）
- 流程：多阶段 Docker 构建（前端 Node.js → Python 运行时）→ 推送 GHCR 与 Quay
- 镜像：`ghcr.io/redgreat/sales-crm-ai`、`quay.io/zrcrm/sales-crm-ai`（secrets：`QUAY_USERNAME`/`QUAY_PASSWORD`）
- 发布脚本：`scripts\dockerbuild.ps1` / `scripts/dockerbuild.sh`（自动计算下一个 `v*` 标签并推送，触发 CI 发布镜像）
- 部署脚本：`scripts/redeploy.sh`（服务器上执行：停容器 → 删旧镜像 → 拉新镜像 → 启动 → 健康检查。默认不改库，需迁移显式加 `--migrate`；指定版本用 `--tag vX.Y.Z`。部署目录自动定位：脚本同级的 docker-compose.yml，否则取上一级；不读环境变量，可用 `--dir` 显式覆盖）
- 镜像仓同步：`scripts\sync-gitlab.ps1` / `scripts/sync-gitlab.sh`（按需双向同步私有仓 `gitlab.lunz.cn`：推送当前分支/标签，`-Pull` 拉取远端分支并合并到本地；首次 `-Url` 配置 remote，凭据用凭据管理器或 `GITLAB_USERNAME`/`GITLAB_PASSWORD` 环境变量；主仓库仍是 GitHub）
- 本地测试：`docker build -t sales-crm-ai .`
