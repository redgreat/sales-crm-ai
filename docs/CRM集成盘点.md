# CRM 集成盘点（P0/P4 前置产物）

> 2026-09-29｜对 `sales-crm-api-service`（Java，master=a3aca9c）与 `sales-crm-admin-ui`（Vue2，main）的只读调研。
> 用途：P4「CRM 集成薄层 + 候选正式写入」的落点依据。AI 侧契约为准（`app/auth.py`、`app/api/routes/*`），CRM 仓库 `docs/研发补充/09、10` 的旧网关方案只作命名参考。

## 1. PRD 体系（补齐 P0 缺口）

- 索引：`docs/README.md`（基线 PRD V0.39.0，2026-09-24）
- 总体：`docs/V0.1.0/瑞赢首个销售闭环/prd.md`
- 模块：`docs/V0.1.0/瑞赢首个销售闭环/prd/00～08`；与 AI 直接相关：
  - **`04-任务销售活动日程与日报.md`**（V1.1）：任务（标题/业务对象/来源活动/责任人/截止/类型/完成标准）、销售活动（类型/发生时间/≥1 业务对象/摘要；正式拜访独立对象）、日报三层（AI 底稿/工作草稿/提交快照冻结）
  - **`05-Agent候选与人工确认.md`**（V1.0.1）：候选→确认→幂等→正式写入；一次沟通默认一条候选活动；「下一步」→候选任务；**活动失败则派生任务不得继续；活动成功后成功任务保留、失败任务单独重试**
  - `07-权限通知审计与异常.md`、`08-PC基础平台与集成治理.md`
- 口径注意：05 写「Coze 一期」、09/10 写「AgentZR 网关」，均已被 AI 仓库当前方案（直连 Provider、点对点）取代。

## 2. 领域模型现状（关键风险）

| 对象 | 表 | Java 实体/接口 |
|---|---|---|
| 客户/联系人/地址等 22 实体 | 有（V001+） | 有（Customer/Contact Controller+Service 完整） |
| 线索/商机（leads、opportunities 等 7 表） | 有（V001） | **无**（bean/Controller 均缺） |
| **销售活动/任务/日报/拜访** | **完全没有** | **完全没有** |
| Flowable | 引擎关闭（`salescrm.flowable-enabled: false`，无 ACT_* 表） | workflow 代码存在但停用 |

结论：P4「候选→正式写入」必须先按 PRD 04 新建 `sales_activities` / `sales_tasks` 等正式对象的最小实现（建表+Service），这超出「薄层」范畴，是 P4 最大工作量项。

可参考的既有实现模式：
- 正式写入+幂等：`CustomerServiceImpl.create()`（TransactionTemplate + idempotency_key 唯一冲突后查回）
- 逐项批量：`ProcessingJobServiceImpl`（job 状态 RUNNING/SUCCEEDED/PARTIAL/FAILED，重试只跑失败项）；**注意 `processing_job_items.item_key` 无唯一约束，AI 幂等键需新建表并显式加唯一索引 (ai_run_id, batch_id, item_id)**

## 3. 认证与权限（薄层挂点）

- 鉴权：Spring Security OAuth2 Resource Server + JWT（UCS 用户中心）；`SecurityConfiguration` → `TokenTransferFilter`（claims 写入 `auth_user_detail` 头）→ `StaffIdentityFilter`（解析→`StaffContext` ThreadLocal，只覆盖 `/api/v1/salescrm/` 前缀）。
- **服务间调用目前没有任何签名/白名单机制**；`spring.security.auth.ignore-url` 是唯一免鉴权通道。
- 数据权限在 service-impl 层：`CustomerAccessContext` + `CustomerAccessPolicy`（R1–R5，本人/协作/团队/公海），客户/联系人可复用；线索/商机无代码。
- AI 薄层方案：新 `AiServiceSignatureFilter`（OncePerRequestFilter，HMAC-SHA256 验签，契约同 `sales-crm-ai/app/auth.py` 的 X-SAI-* canonical 串）+ 配置 `salescrm.ai.*`；`X-SAI-User-Id` → staff 解析 → `StaffContext`（注意 finally clear）。

## 4. 可复用查询接口（AI 事实查询）

- `CustomerController`：`POST /api/v1/salescrm/customers/page`、`GET /{id}`、`GET /search?q=`
- `ContactController`：`GET /api/v1/salescrm/customers/{customerId}/contacts`、`GET /{contactId}/mobile`（脱敏）
- `MeController`：`GET /api/v1/salescrm/me`
- 事实查询薄层直接调 `CustomerService.page/get` + `ContactService.listByCustomer`，以 AI 调用对应 staff 构建 AccessContext 保持同权限。

## 5. 前端（Vue2）集成成本：低–中

- axios 封装（`src/utils/request.js`，自动 token/驼峰转换）、占位路由 `/tasks /visits /reports /processing` 等待替换；API 文件按域组织（`src/api/crm/*.js`）、页面 `src/views/crm/<域>/`。
- AI 正式入口建议：`src/api/crm/ai.js` + `src/views/crm/ai/{candidates,runs}.vue`；联调前端仍用本项目 Svelte（frontend/），Vue2 内入口属 P4 之后。

## 6. P4 落点清单（建议，动工前按此拆任务）

1. `salescrm-web/filter/AiServiceSignatureFilter.java` + `SecurityConfiguration` 注册 + `AiServiceProperties`（prefix `salescrm.ai`）
2. `application-dev.yml`：`salescrm.ai.*`（key-id/secret/clock-skew；如独立验签则 ignore-url 加 `/api/v1/salescrm/ai/**`）
3. `salescrm-api/controller/AiIntegrationController.java`：`/api/v1/salescrm/ai/context/*`（事实查询）、`/candidates/import`、`/runs`（出站代理）
4. `AiIntegrationService(+Impl)`：候选导入仿 ProcessingJob 模式，活动先于任务、活动失败停止派生、部分成功逐项重试
5. Flyway `V010__ai_candidates.sql`：候选表（唯一索引 (ai_run_id, batch_id, item_id)，状态 PENDING/CONFIRMED/IMPORTED，重复导入不覆盖人工编辑）+ **PRD 04 正式活动/任务表的最小实现**
6. `salescrm-service-impl/ai/`：AiApplicationService + 出站 HMAC 客户端（调 AI 8310 端口）
7. 分支：从最新 master 建 `service_dev_ai`（前端 `uf_dev_ai`）；master 已前移至 a3aca9c，动工前先 rebase 基线
