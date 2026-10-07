# CRM AI 接入需求文档（Python 重建版）

> V2.2｜2026-10-02｜需求统一基线：吸收移动端 V2.6 决策、双端契约及 ASR/OCR/OSS → LLM 流程。实现与验收证据统一见 [实施计划](AI接入实施计划.md)；本文新增目标不代表已经实现。
> 本文替代旧 Java AI 方案；业务规则继承瑞赢首个销售闭环 PRD。配套：[实施计划](AI接入实施计划.md)。

## 1. 决策与仓库职责

新 AI 项目现为 `D:\github\CRM\sales-crm-ai`，模板现为 `D:\github\CRM\AgentZR`。采用 FastAPI、LangChain 模型适配、LangGraph 编排及 PostgreSQL 持久化 checkpoint；依赖以仓库锁文件和实测为准，不照搬模板宽泛版本范围。

CRM 后端 `D:\github\CRM\sales-crm-api-service` 保持现有 Java 业务系统，不改写为 Python；CRM PC 前端 `D:\github\CRM\sales-crm-admin-ui` 保持 Vue 2.7 + Vue CLI。当前 AI 开发联调使用本仓库 Svelte 前端，生产企业微信 H5 为主要 AI 操作端，H5 实际代码仓库和登录契约需在移动端工作包确认。用户的“不用 Java”指 AI 实现迁往 Python，不表示重写整个 CRM。

一期移除 smart-gateway、open-gateway、多通道机器人、平台 API Key 签发、Token 计量/费用/配额和平台门户。保留服务间认证、权限、基础并发限制、审计和错误处理，它们不是要删除的网关业务。Coze 等外部知识组件可选，不强制接入。

新项目管理所有新需求、计划、入口及联调脚本。模板仅作为来源，不修改 AgentZR；不复制其 .git、环境密钥、数据库数据、node_modules、虚拟环境或云发布配置。不继承其“模型必须经 Open Gateway”的规则，新模型调用直接从 Python Provider 出站。

## 2. 模板复用与去网关边界

已核验模板提交 `b14adec594a0c1f2b02f046ee028317501ccffc9`：存在 LangGraph StateGraph、`shared/persistence/checkpoints.py` 的 AsyncPostgresSaver、显式 checkpoint 初始化及连接生命周期管理。此为源码证据，不代表新项目已经验证这些能力。

- 选择性复用：`shared/agent_runtime`、`shared/agent_core`、持久化/checkpoint、合同类型、安全/日志、知识检索及对应测试中与 CRM 范围相关的部分。
- 适配后复用：problem_agent 图的节点组织方式，不照搬其业务流程；模型运行层拆出直连 Provider；技能执行只保留受限 CRM 查询及必要专项调用。
- 不迁入：两个 gateway 服务、shared/open_platform、平台计费及通道模型、平台租户/配额管理、无关政策/报表 Agent、独立审批平台和 AgentZR 管理前端。
- 模板 `shared/model_runtime/gateway.py` 引用 Open Gateway；`factory.py` 有 usage/cost 持久化。必须清理传递依赖、回调、数据库模型与环境变量，不是仅删部署服务。供应商响应中的 usage 不写日志、消息存储或 checkpoint。
- 模板 README 的部分记忆文档链接已不存在，以真实源码/测试为准。源码迁移需登记来源文件、提交、许可证与改造理由；模板未提交改动不默认纳入。

## 3. 总体交互与信任边界

```mermaid
flowchart TD
  UI[生产企业微信 H5：主要 AI 操作端；PC：轻量状态入口] --> CRM[CRM 后端：登录、授权、AI 集成接口]
  CRM --> AI[Python FastAPI：CRM 专用 AI 服务]
  AI --> Graph[LangGraph：抽取、追问、检索、摘要]
  Graph --> Model[直连模型或 ASR/OCR 服务]
  Graph --> DB[(AI PostgreSQL：会话、运行、checkpoint)]
  Graph --> Read[CRM 有权事实查询接口]
  Read --> CRM
  Graph --> Result[结果及引用、候选建议]
  Result --> CRM
  CRM --> Candidate[(CRM 候选、确认、正式写入记录)]
  Candidate --> User[发起用户编辑并确认]
  User --> Service[CRM 正式活动及任务 Service]
  Service --> Biz[(CRM 正式业务库)]
```

“直接对接”是 CRM 与 Python 点对点，不经过 AgentZR 网关。浏览器默认通过 CRM 同源集成接口访问，不持模型密钥或长期服务凭据。该集成接口只处理本项目业务，不建设通用模型网关。

CRM 服务端鉴权后传操作者、受益人、对象范围、来源版本和关联请求号；使用限时签名/服务凭据保护服务间请求，Python 校验签名、audience、时效及重放。身份字段不能由浏览器任意指定。Python 回查 CRM 使用最小范围的用户授权上下文，CRM 每次重新鉴权，不允许 Python 直接查业务库或持全员权限。

AI 服务不可绕过本人确认写正式任务。首次集成采用 CRM 拉取 Run 结果并幂等导入候选；回调如后续启用需认证、重放保护、幂等及对账。不设计跨库事务，不以网络响应成功冒充正式写入成功。

## 4. 全量业务能力与调用方法

各能力共用创建 Run、查询结果、错误与取消契约，不按每个功能重建 Provider。

1. **沟通整理**：文字/粘贴/授权来源 → CRM 装配事实 → `communication.extract` → 活动及零到多任务建议 → CRM 候选 → 本人确认。对象精确匹配优先，歧义人工选，无匹配不自动建对象。
2. **会议批量任务**：同一会议批次复用抽取图；逐项补参/确认。先活动后任务，活动失败不写任务，部分任务失败不回滚成功项。
3. **对象匹配与相似任务**：CRM 有权集合及确定性检索为主，模型辅助名称/意图；相似仅提示，技术重复用幂等控制。
4. **日报**：`daily.draft` 读取当日正式事实，不取未确认候选；返回 CRM 日报底稿，保留工作草稿、首次底稿、提交快照，人工提交后冻结。
5. **客户/线索/商机摘要**：`object.summary` 返回只读状态、事实、缺失、建议及逐条引用，不覆盖正式字段。
6. **对象内追问**：`object.qa` 在对象绑定会话中使用有效历史与当前事实；每轮鉴权，事实/推断分开。
7. **轻量跨对象问答**：`business.qa` 仅调用白名单查询，限制条数/时间窗；不让模型生成任意 SQL，指标由 CRM 计算。
8. **知识问答**：`knowledge.qa` 检索已发布且有权版本，返回引用及版本；过滤发生在内容进入模型前，不允许先读无权全文再隐藏引用。
9. **会前准备**：`meeting.prepare` 主动触发，组合正式沟通/待办及有权知识；不自动建会议或对外发送。
10. **今日任务总结**：`today.summary` 归纳确定性底单，不创建任务或另造权威优先级。
11. **主管关注**：`manager.focus` 只读团队权限内底单，不评分、不作绩效结论、不自动派任务。
12. **录音**：ASR → 带时间锚点的校对文本 → 抽取；说话人不直接映射员工。
13. **图片**：OCR/经验证的多模态 → 原图锚点 → 抽取，关键数字日期需确认。
14. **文件**：普通文本解析优先，扫描页 OCR，保留页码/版本后复用抽取。
15. **大圆文档**：身份映射及授权读取 → 固定文本版本 → 抽取；来源撤权后不可继续使用。
16. **知识发布同步**：CRM 知识发布/停用 → 授权同步任务 → AI 索引映射，失败可重试；撤权先在可用集合中生效，不等待外部删除。
17. **对话建档（V2.6 已纳入一期）**：`customer.draft`、`contact.draft`、`lead.draft`、`opportunity.draft` → 权限内先匹配再新增/补充 → 必填追问 → CRM 草稿候选 → 本人确认 → CRM 正式 Service。查询客户/其他信息复用现有问答能力；不新增 AI 自动建档、正式拜访写入或绕过查重的工具。

只有今日任务总结、主管关注、日报底稿允许按固定时间预生成，不主动推送。调度入口由 CRM 按受益人发起，Python 持久执行，不在两端重复调度同一业务任务。其他能力主动触发。30 秒内应有结果或明确处理中/失败状态，长任务离页可查询。

任务接收/验收、线索派领、商机规则、签到通知、权限审计和工商结构化查询仍归 CRM 确定性业务。增强输入逐项 POC 与启用验收，不能关闭后宣称全部完成。

## 5. 可靠执行与多轮：必做而非扩展项

### 5.1 可靠执行

LangGraph + PostgreSQL checkpoint 负责图状态保存和中断恢复，不自研图引擎。仍必须实现持久队列/扫描、认领租约与执行代次、重启恢复、重试耗尽及取消后的迟到隔离；checkpoint 不是任务调度器。

Run 与必要来源引用原子创建，幂等重复返回原 Run；节点保存已完成输出引用。外部调用不得占长数据库事务。模型返回后、结果持久化前崩溃可能导致重调，要明确处理及观测，不能宣称外部恰好一次。

CRM 导入结果使用稳定 `(ai_run_id, batch_id, item_id)` 唯一键及结果版本/hash；重放不覆盖人工修改。正式写入后返回丢失时按业务幂等键查询原结果。checkpoint 与 CRM 提交不是同一事务，不保存“已写入”虚假状态。

记录图版本、状态 Schema、模型/Prompt/配置版本、线程与执行代次。部署升级旧 checkpoint 须兼容或显式迁移/阻断；失败不降级无状态图。初始化/迁移显式执行，不在服务启动自动改库。

### 5.2 会话、短期记忆与补参

聊天历史、选入模型的窗口记忆、结构化流程状态分开。CRM 用户/对象绑定 conversationId，后端映射 LangGraph thread_id；每轮独立 requestId/runId，thread_id 不构成权限凭证。

保留有限完整轮次和必要摘要；字段值、缺项、等待问题、对象/来源/候选版本放结构化状态。补参 → 校验 → 展示最终候选 → 本人确认，不以聊天回答替代确认。

使用图 interrupt/resume 或等价原生机制实现 WAITING_INPUT/WAITING_CONFIRMATION，等待不占线程/租约。恢复前重新鉴权、校验状态版本；双标签页、重复消息和旧 resume 必须拒绝或返回原结果。

每轮重取当前有权事实；撤权历史/摘要/checkpoint 内容不可继续送模型，必要时终止旧会话另起线程。支持清空、结束、过期、切换对象和保留期清理；不做跨会话长期画像。

## 6. 数据库与页面归属

**独立 AI PostgreSQL 库**（拟名 sales_crm_ai）只存 AI 运行数据：模型/知识接入配置、能力绑定、conversation/message、Run/尝试、输入输出引用、图线程映射、框架 checkpoint、知识外部索引映射及脱敏操作审计。框架表由锁定版本的专用迁移管理，不手改其内部结构；Redis 可作缓存/唤醒，不作唯一事实源。不创建平台用量、计价和网关表。

**CRM 库**保留身份/权限、客户线索商机、知识主档及发布权限、活动任务、日报快照、候选/确认/正式写入幂等和审计。旧 Java 分支上的设计不等于主线已有表，P0 逐项核验。不得直接复制旧迁移编号到当前主线。

前台以 CRM 为唯一业务入口：新增 AI 接口联调页面；技术配置可在同入口受限页签维护模型、知识组件、能力绑定和健康状态；业务配置继续能力/来源/试点/预生成四页签。联调页面与销售页面分权，技术权限不等于全员正文可见权限。

## 7. CRM 集成对接方案

### 7.1 系统分工

```mermaid
mindmap
  root((CRM AI 系统))
    CRM Java 后端
      身份认证与权限
      正式业务数据
      候选确认与写入
      审计与幂等
    AI Python 服务
      模型调用与编排
      会话与记忆
      Run 队列与执行
      候选生成与追问
    前端
      CRM Vue2 业务入口
      Svelte AI 联调页面
      统一身份与权限
```

| 组件 | 职责 | 禁止 |
|------|------|------|
| **CRM Java** | 身份认证、权限过滤、正式业务写入、候选确认、审计 | 直连模型供应商、持有模型 Key |
| **AI Python** | 模型调用、Prompt 编排、会话管理、候选生成、Run 队列 | 直写 CRM 正式表、绕过权限 |
| **前端** | 用户交互、候选展示与确认、Run 状态展示 | 持有服务密钥、直接调用 AI |

### 7.2 集成架构

```mermaid
flowchart LR
    subgraph 前端
        H5[H5/PC 前端]
    end
    subgraph CRM Java
        AUTH[JWT 认证]
        BIZ[业务 Service]
        CAND[候选与确认]
        FACT[事实查询 API]
    end
    subgraph AI Python
        API[FastAPI]
        GRAPH[LangGraph]
        RUN[Run 队列]
        CHECK[(Checkpoint)]
    end
    subgraph 模型
        LLM[直连 Provider]
    end

    H5 -->|用户 JWT| AUTH
    AUTH --> BIZ
    AUTH -->|代理请求| API
    API --> GRAPH
    GRAPH --> LLM
    GRAPH --> CHECK
    CAND -->|CRM 拉取 Run 结果| API
    CAND -->|确认写入| BIZ
    API -->|HMAC 签名回查事实| FACT
```

### 7.3 鉴权机制

```mermaid
flowchart TD
    A[用户登录 CRM] --> B[CRM 签发 JWT]
    B --> C[前端携带 JWT 调用 CRM]
    C --> D[CRM 验证 JWT 并解析身份]
    D --> E[CRM 代理调用 AI 服务]
    E --> F[AI 验证 HMAC 签名]
    F --> G[AI 使用 CRM 传入的身份]
    G --> H[AI 回查 CRM 事实 API]
    H --> I[CRM 验证 HMAC 并过滤权限]
    I --> J[返回脱敏事实给 AI]
```

**鉴权层次：**

| 层次 | 机制 | 说明 |
|------|------|------|
| 用户 → CRM | JWT Bearer | CRM 签发，含用户身份与权限 |
| CRM → AI | HMAC-SHA256 签名 | 含 key_id、timestamp、nonce、user_id |
| AI → CRM | HMAC-SHA256 签名 | 回查事实 API，最小权限 |
| 防重放 | nonce + 有效时间窗 | AI 侧已使用数据库 nonce；Java 当前为实例内 nonce，集群一致性待 P4 验证。时窗以冻结配置为准，不以本表替代真实契约 |

### 7.4 核心流程

```mermaid
sequenceDiagram
    participant U as 用户
    participant C as CRM
    participant A as AI 服务
    participant M as 模型

    U->>C: 提交沟通内容
    C->>A: 创建 Run（HMAC 签名）
    A->>M: 调用模型抽取
    M-->>A: 返回候选建议
    A-->>C: Run 完成，返回结果
    C->>C: 幂等导入候选
    C-->>U: 展示候选待确认
    U->>C: 确认/修改候选
    C->>C: 校验权限与必填
    C->>C: 写入正式活动/任务
    C-->>U: 返回正式对象
```

### 7.5 数据边界

```mermaid
flowchart TB
    subgraph CRM 库
        T1[(客户/线索/商机)]
        T2[(任务/活动/日报)]
        T3[(候选与确认记录)]
        T4[(审计日志)]
    end
    subgraph AI 库
        D1[(Run/会话/消息)]
        D2[(Checkpoint)]
        D3[(知识索引映射)]
    end
    T1 -.->|只读，经 API| D3
    D1 -.->|只读，经 API| T1
    T3 -->|正式写入| T2
```

**核心原则：**
- AI 库不存正式业务事实，只存运行数据与授权索引
- CRM 库是正式事实唯一来源
- 跨库只通过 API，不直连数据库
- 候选 ≠ 事实，必须人工确认

## 8. AI 专用接口联调页面

当前实现为本仓库 `frontend/` 独立 Svelte 联调应用，服务端 `/playground/api` 注入测试身份，仅 dev/test 且显式配置启用，不是正式 CRM 用户授权。生产 H5/PC 使用 CRM 登录与业务权限；不得把联调代理或测试身份搬到移动端。开发联调页面不能作为 PC 完整 AI 操作端交付，不迁入 AgentZR Vue3 页面。

提供文本抽取、会议补参、连续问答、知识引用、摘要/日报、ASR/OCR/文件/大圆测试入口；按能力分组，未实现项标记未实现，不伪造成功。展示 Run/会话/步骤状态、请求耗时、缺参/引用、脱敏请求响应、失败重试与恢复；不展示 Key、完整内部堆栈、Token 或费用。

明确区分 Stub、真实 Provider、真实 CRM 三种模式。Stub 不写正式业务；真实写入仅限明确测试环境、测试对象及用户逐项确认。测试页不得绕过业务鉴权、确认和幂等。对比不同 profile 仅给技术角色，普通销售不选模型。

## 9. 一键启动脚本要求

脚本放新项目 `scripts/dev.ps1`，支持 start/status/stop、可配置 Python/前端/CRM 路径和端口；当前默认 AI API/worker 与本仓库 Svelte 联调端，生产 H5/CRM PC 的启动及接入另按其工程约定。CRM 后端默认连接指定开发环境，可选显式参数启动本地 Java CRM。所谓“启动前后端”不意味着把 CRM 后端一起改成 Python。

启动前检查 Python 虚拟环境、锁定依赖、Node/包管理器、端口、环境配置、PostgreSQL 迁移和 CRM 健康；不自动安装/升级全局工具、不建真实库或静默执行迁移。缺依赖输出操作提示，失败清理本次启动进程。

进程后台隐藏窗口运行，日志脱敏落本地忽略目录，记录 PID/启动时间/命令归属；stop 仅终止本脚本启动且验证身份的进程，不能按端口杀其他服务。端口冲突清晰失败；重复 start 幂等，status 区分已运行和不健康。Node 版本以 Vue2 工程实测确定，不盲目用 package engines 的过宽范围。

## 10. 验收与排除范围

必须通过真实 PostgreSQL 重启恢复、旧执行者迟到、重复 resume、跨用户会话、历史撤权、CRM 超时重放、部分任务成功、知识生成前过滤和手工降级测试；分别记录框架测试、Stub、真实模型、真实 CRM 闭环证据。

不做：Java AI SDK/公共 JAR、两个网关、平台计费配额、多通道机器人、任意 SQL、自主正式写入、公司级模型运营平台。模板“已经完成”的描述不可当本项目验收。本文定义目标，实际完成度以实施计划和可复核测试记录为准。

## 11. 移动端 AI 入口与 PRD 对齐（2026-09-29 增补）

### 11.1 需求基线与评价

移动端现行评审基线为 `sales-crm-api-service/docs/瑞赢移动端1.0/` 的 V2.6 产品版（2026-09-30）；09-29 的 `瑞赢1.0-移动端原型` V2.1 仅为历史快照。页面研发描述、按钮文案和版本需一致，旧 notes 缺少的正文不能自行补推。后端 [05 AI](../../sales-crm-api-service/docs/V0.1.0/瑞赢首个销售闭环/prd/05-Agent候选与人工确认.md) 与 04/07/08 继续约束业务权限、状态和正式写入；Coze 必选技术条款按本项目 Python 决策替代。

评价：移动端稳定 page-id、就近业务入口、上传归属和纪要/任务分离值得保留。V2.6 已收敛多数状态、通知及采集冲突，并明确扩入对话建档；不得继续按旧版“一期不含建档”实施。尚未确认事项集中在第 11.5 节，不能用原型按钮代替生产接口和真机验收。本次归并不修改移动端/后端源 PRD。

### 11.2 页面到能力和数据流的映射

- **M01 工作台、M09 团队**：分别展示 `today.summary`、`manager.focus` 的规则底单归纳、截至时间及有权源对象；不创造优先级/绩效评分或自动派任务，前端只读。
- **M19 浮动助手**：统一已批准能力入口，带当前对象上下文，支持 `object.summary`、`object.qa`、`business.qa`、`knowledge.qa`、`meeting.prepare`；主动切换对象须确认新上下文并使旧候选失效，不能串用原会话材料。
- **M19/M20 助手及快捷创建**：新增客户/新增或补充联系人/新增线索/新增商机走第 4.17 项草稿能力，查询客户/其他信息复用问答图。查询类只读；动作类及新增补增类都先出草稿、再本人确认，不直接改数据。主管派任务/约会议也不得绕过 CRM 正式流程；正式拜访写入不随建档能力自动扩入。
- **M10/M11 沟通与上传**：文字/有权原件 → 来源 ID/版本 → 格式校验及 ASR/OCR/文件解析 → 授权对象匹配 → `communication.extract`。Top3 可为推荐展示，不是固定正确率保证；不显示复杂置信度，不强绑无匹配对象。
- **M03 AI 待确认**：CRM 候选列表/详情/编辑/忽略/确认，展示活动和任务、证据/缺参/人工值；确认后的正式对象与失败回执来自 CRM，不从 AI Run succeeded 推断已写入。
- **M12～M15 拜访**：签到/签退是 CRM 事实；用户主动提供授权录音/速记后生成纪要建议。正式拜访总结保存与派生销售活动/任务的目标映射由 P4 冻结，不把活动候选直接当拜访完成，不重复生成正式事实。
- **M23～M25 会议**：会议信息由 CRM 创建；用户主动上传/输入并发起整理 → 同来源批次候选 → 补参 → 勾选确认 → 活动先写、任务逐项写。取消自动追踪任务规则；会议录音 SDK/实时转写待独立范围确认。
- **M16 日报**：`daily.draft` 使用正式事实，首次 AI 底稿/工作草稿/提交快照分别保存；刷新保护人工编辑，提交后冻结。AI 预生成不推送，日报未提交业务提醒按白名单处理。
- **M17/M18 知识**：CRM 负责列表/正文/发布权限，`knowledge.qa` 生成前过滤；引用包含文档 ID/版本/定位，打开/下载重新鉴权。V2.6 保留工号水印和企微转发留痕，归 CRM/H5 实现；内部资料不可转发，链接转发不授予额外读取权限，不能把水印当授权。
- **M02/M27 消息、M04～M08/M21/M22 正式任务/日程**：走确定性 CRM Service，不新增模型调用来改变接收/验收/日程状态。深链可携带业务定位，但不能携带可冒用服务身份。

### 11.3 移动端统一交互契约（目标，待 P4 冻结）

```mermaid
flowchart TD
  Entry[业务详情或 M19：带 page-id 和对象引用] --> Auth[CRM 解析 H5 身份并鉴权]
  Auth --> Source[合法来源及版本；前端稳定请求号]
  Source --> Run[Python Run 与服务端会话]
  Run --> Wait{需要补参吗}
  Wait -->|是| Ask[展示缺项；按 state_version 恢复]
  Ask --> Run
  Wait -->|否| Kind{结果类型}
  Kind -->|活动或任务| Draft[CRM 幂等导入候选；展示证据和影响]
  Draft --> Confirm[本人勾选并确认最终值及版本]
  Confirm --> Write[CRM 权限和幂等：先活动后逐项任务]
  Write --> Result[逐项正式 ID 或失败；原键重试]
  Kind -->|日报| Daily[工作草稿到本人提交快照]
  Kind -->|摘要或问答| Read[只读答案、来源和截至时间]
```

页面最少携带/保存：page-id、业务 capability、对象/来源引用、客户端请求号、服务端 Run/会话/候选 ID、当前状态版本及返回路径；员工身份和授权范围由 CRM 解析。现有 `/mobile/*`、`/assistant/*` 为草案接口，不等于现有 Python/Java API，需维护映射后联调。

离页/收起不自动取消任务，刷新按已保存 ID 查询恢复，不重新 POST 创建；只有新的主动生成产生新请求号。重复点击/弱网重投复用原键。30 秒内有明确状态，上传/转写长任务可继续查询；分开未生成/处理中/待补参/待确认/部分写入失败/只读完成/无证据/失效/不可用。

确认时提交明确选中项、候选/来源版本及最终值；同一会议批次才可批量，不把 override 列表当作勾选范围。本人补参不等于确认，责任人与截止时间缺失需显式补齐。技术错误重试与重新生成分开，业务冲突提示重新核对，不重放旧操作。

### 11.4 权限、生命周期与移动端验收

会话在服务端持久化，前端缓存只是恢复定位；会话关闭/过期不自动使业务候选过期，候选超过历史阈值仍可在有权条件下处理。来源更新/对象切换/撤权才触发对应候选失效。历史正文、摘要、checkpoint 和文件 URL 都在再次展示/使用前校验权限；知识新检索过滤不能代替历史撤权。

验收新增：真实 H5 登录而非 playground 身份、刷新/离页/弱网重投、跨用户深链、同名有权对象选择、对象切换、待补参恢复版本冲突、活动失败及任务部分成功、写入超时原键重试、日报编辑后刷新、文档停用/撤权后历史查看及再次追问。

不因 Svelte 联调功能完成而标记移动端完成。移动端草案更新后，以 M 编号记录决策、修改版本与回归用例；未决扩范围入口默认不启用。对话建档按 V2.6 已批准范围推进，但代码完成不等于上线验收；后台自动录音与不受控外部分享仍不批准。

### 11.5 M-01～M-12 决策合并台账

以下合并 09-29 建议、09-30 AI 落实和 10-01 V2.6 复核；后续只在此更新决策，在实施计划更新进度。

| 编号 | 当前有效规则 / 待确认点 |
|---|---|
| M-01 | 候选待确认/已确认/已忽略/已失效；超时进入历史待处理，不按 24h 自动失效；来源更新、撤权、对象失效由系统判定；忽略理由可选，不自动回流训练。会话 TTL 与候选生命周期分开。 |
| M-02 | 不派生“追踪任务”，协作人不隐式变多个任务；负责人唯一。批量确认暂按后端“同会议来源、逐项校验、部分成功”执行；M03 跨来源混选是否允许待产品+后端确认。 |
| M-03 | 一期四类建档草稿+两类查询已经确认；先匹配、缺项追问、权限内正式 Service 写入、来源标记。联系人“补充”所需版本/覆盖范围需随正式接口验收，不能以新增路径代替。 |
| M-04 | 仅主动录音/授权上传；不依赖工作手机，不做后台采集和实时会议转写。用户可拒绝录音。拜访签退可按明确交互请求生成草稿，真实性验证只作附注。 |
| M-05 | 谁提炼谁确认，主管不能代确认；候选确认、任务接收、任务验收分开。本人任务 OPEN、指派他人 PENDING_ACCEPT；验收人与责任人不同且按 CRM 快照规则冻结。 |
| M-06 | 服务端持久会话/checkpoint，收起与离页保留，刷新按 ID 恢复；前端缓存不是唯一事实源。 |
| M-07 | 当前待确认/历史待处理/写入失败独立视图，保留证据、最终确认值和逐项结果；失败原键重试、重新鉴权，不覆盖人工值。 |
| M-08 | 系统事件白名单推送，仅需本人立即动作事件；AI 普通候选/建议仅站内留档。无 L1–L4、个人偏好、静默时段或时间窗合并；日报未提交业务提醒与 AI 生成完成推送分开。 |
| M-09 | 首次底稿/工作草稿/提交快照三层；跨端共用，刷新不覆盖人工编辑并二次提示；时间取 CRM 配置（产品默认 18:30），无事实手填说明，展示 as_of/facts_count，主管批示回流留言。 |
| M-10 | 只读已发布当前版本，检索前授权，正文/下载重鉴权。角色×业务线交集/并集待产品决定；CRM 展开 user_id，AI 只接受 public/users，拒绝组织维度 scope，不擅自扩大。 |
| M-11 | 独立日程有效/已取消/已删除；任务/拜访/会议是只读投影；AI 纪要不替代签到签退或正式拜访完成。 |
| M-12 | 能力目录提供能力码、只读标识、图/Prompt 版本、上下文、入口条件及返回路径；生产页面结合用户权限显示。今日任务只归纳确定性底单，不恢复“今日三件事”或模型排序。 |

其余待确认：① M20“丢弃无痕”应明确为不产生正式业务数据，Run/候选仍按保留期审计；② 生产 H5 形态已确认（2026-10-07）：H5 前端由移动端团队按 api-service 仓设计文档开发（提交 8c20dfbc），后端底座已落地——企微静默登录（code→CRM 自签会话 JWT）、企微 userid→staff 映射走 identity_bindings（provider=WECOM）、H5 业务接口，AI 调用经 CRM 代理；真机验收待 H5 前端产出；③ 页面路由与真实 API 的映射（待 H5 前端产出后冻结）。未决权限组合失败即关闭，未批准跨来源批量不开放。

知识版本由 CRM 声明 `is_current`，不按 v9/v10 字符串排序；当前兼容发布时间回退不代表严格版本契约验收。历史再入模前过滤失权引用，同时还需验证旧正文/摘要/用户消息中的撤权材料；只过滤 assistant 引用不代表所有历史授权已闭合。未确认候选不得作为摘要/问答事实，相关度排序不能参与授权判定。

## 12. 文件、ASR/OCR 与 LLM 整理的统一流程（2026-10-02 决策）

### 12.1 职责与两类输入

**文件管理和权限归 CRM；识别与 LLM 编排归 AI；H5 负责采集、展示和确认。AI 不限制为只接最终文字。**

| 组件 | 一期职责 |
|---|---|
| H5/前端 | 主动录音/拍照/选文件、上传与处理进度、原文和优化结果对照、编辑确认、离页恢复；不持长期云凭据。 |
| CRM API | 身份/对象/来源权限、文件归属及上传授权、元数据/保留期、AI Run 代理与结果查询、人工确认后的业务写入；优先复用已有文件服务，是否满足需盘点。（**2026-10-07 盘点结论与文件契约冻结见 12.5**：CRM 无既有通用文件服务，新建 resource_info + 附件公共组件。） |
| Python AI | ASR/OCR/文本解析 → 原始文本与证据 → LLM 整理/抽取 → 补参/候选；保存阶段输出与供应商任务引用，负责恢复及有限重试。 |
| 私有 OSS | 存原始音频/图片/附件；业务归属在 CRM。AI 可有受限临时文件空间，必须规定清理、授权和保留期，不复制一套业务附件系统。 |

输入契约需同时支持：① `text`（手输/粘贴/外部已识别文本，附来源信息）；② `file_ref`（`fileId + sourceVersion + 处理类型`，附业务上下文）。这些是目标字段，不宣称已有对应生产 API；由 P4/P5 冻结路由、Schema、大小/时长限制和错误码。

文件归 CRM 不要求 Java 转发所有字节：具备条件时 CRM 签发短时上传授权，H5 直传私有 OSS，CRM 校验对象、大小/类型及归属后登记可处理文件；否则复用 CRM 现有受控上传接口。不要重复上传到 Java 和 Python。已有 ASR/OCR/OSS 适配层保留在 AI，联调上传入口仅 dev/test，不能直接作为生产授权入口。

### 12.2 端到端交互

```mermaid
flowchart TD
  H[H5 主动录音或选文件] --> C[CRM 鉴权并授权上传]
  C --> O[一次上传到私有 OSS]
  O --> F[CRM 校验并登记 fileId 和来源版本]
  F --> R[CRM 发起 AI Run：文件引用和业务上下文]
  T[H5 文字或已有识别文本] --> CT[CRM 鉴权并发起文字 Run]
  CT --> L
  R --> A[AI 获取受控访问权限并校验输入]
  A --> X[ASR 或 OCR 或文本解析]
  X --> S[持久保存原始识别文本和来源锚点]
  S --> L[LLM 整理或抽取：保存独立结果版本]
  L --> V[经 CRM 查询；H5 展示原文与建议并允许修改]
  S -->|LLM 失败仍可使用原文| V
  V --> U[用户确认最终文本或业务候选]
  U --> B[CRM 重新鉴权和校验版本后写正式业务]
```

AI 任务应能区分待处理、识别中、整理中、待补参、完成、失败、取消；上传状态与 Run 状态分开，阶段字段映射现有 Run，不平行新建另一套任务引擎。30 秒内返回结果或明确处理中，长录音异步执行，刷新/离页不重复提交。

### 12.3 结果可靠性与安全

- 分别保存**原始识别文本、LLM 优化文本、用户确认文本**及版本关联，不用优化结果覆盖识别原文。原件归 CRM；AI 中间结果仅作受控运行材料；正式确认值与业务快照归 CRM。
- 识别结果保留音频时间锚点/图片区域/页码等实际可得证据；`speaker_id` 不自动映射员工。已识别外部文本无锚点时标明缺失，不伪造。
- 优化用于断句、分段和去重复，不静默改写姓名、金额、日期及业务承诺；不确定处标注并请用户核对。推断与识别事实分开，关键字段必须人工确认。
- ASR/OCR 成功后重试 LLM 复用原输出；保存供应商任务 ID，超时先查原任务，无法避免的重复外部调用要披露。checkpoint 不替代供应商幂等、阶段持久化、租约和取消迟到隔离。
- 文件/来源/识别或 Prompt 版本变化时显式重新处理，不能命中旧结果；失败区分上传、识别、整理、CRM 正式写入，不一律重新生成。
- 持久化文件标识而非永久保存签名 URL；短时链接过期后重新鉴权获取，时效覆盖供应商实际拉取及重试窗口。拒绝任意 URL/内网地址读取；下载校验、重定向及大小限制由受控适配器落实，URL 与密钥不落日志/checkpoint。
- OCR 技术上可使用图片二进制，不强制额外复制到 OSS；生产需要原图留证时仍使用 CRM 文件归属。ASR 当前录音文件适配器使用可访问 URL，不将其当成所有 ASR 产品的通用限制。

### 12.4 页面落点

M10/M11 沟通上传、M13～M15 拜访录音、M23～M25 会议输入共用该链；M16 语音补充日报只把识别/整理结果送草稿编辑，不自动提交日报。测试页增加原文/优化文本/锚点/阶段失败及重试展示；不新增工作手机自动采集、实时转写、声音身份识别或未经确认的业务写入。

### 12.5 文件契约冻结（2026-10-07，P5-INPUT 任务 1）

**架构决策（2026-10-07 用户确认）**：业务附件归 CRM——zrcrm 主库建 `resource_info` 登记表，上传/下载公共组件在 `sales-crm-api-service` 实现；AI 不自建附件系统、不重复搬运文件（AI 自有的 OSS 适配器仅用于其临时识别材料，不承载业务附件）。AI 侧识别产物（原文/整理文/确认文三层）持久化仍归 AI 库，属 P5-INPUT 任务 2。

**职责与归属**

| 项 | 归属 |
|---|---|
| 文件本体存储 | OSS 私有桶（生产 `salescrm.oss.mode=oss`，凭据经环境变量注入）；dev 联调 `mode=local` 落 `{salescrm.files.dir}/resources/`，不提供外部 URL |
| 元数据/授权/撤权 | CRM `resource_info`（CHAR(12) RS 前缀主键；biz_type/biz_id 业务绑定、version 来源版本、ACTIVE/REVOKED 撤权、软删） |
| 身份与权限 | 业务端上传/绑定/撤权/下载=JWT 本人；AI 访问=HMAC（AiServiceSignatureFilter），仅本人附件可见 |
| AI 侧 | 持久化文件标识而非签名 URL；每次使用前经 CRM 受控访问校验；撤权/删除后不可再取 |

**file_ref 契约**：`{file_id: "RS"+10位, source_version: int, processing: asr|ocr|parse}`。文件或版本变化必须显式重新处理，不命中旧结果。

**双端端点**

- 业务端（JWT）：`POST /api/v1/salescrm/resources/upload`（multipart，biz_type/biz_id 可选——AI 链路先传后建档再 bind）、`POST /{id}/bind`、`POST /{id}/revoke`、`GET /{id}`、`GET /{id}/file`（流式返回）。
- AI 端（HMAC，凭据=crm-ai + AI_SERVICE_AUTH_SECRET，双向同钥）：`GET /api/v1/salescrm/ai/integration/files/{id}` → `{found, file:{id,file_name,content_type,file_kind,size_bytes,source_version,status,biz_type,biz_id,created_at}, access:{type: signed_url|inline|none, url?, expires_in_seconds?, content_path?}}`；`GET .../{id}/content` 仅 local 模式取字节（oss 模式 CRM 返回 400——AI 直拉签名 URL，不经 CRM 中转）。
- 归一语义：非本人/不存在/已删除 → `found=false`（HTTP 200，不泄露存在性）；撤权（本人可见）→ `found=true + status=REVOKED + access.type=none`。签名 URL 短时有效，过期重新调用获取。

**分级上限（初版默认值，联调后按供应商实际限额校正）**：音频 200MB（mp3/wav/m4a/aac/amr/ogg/flac）、图片 10MB（jpg/jpeg/png/bmp/webp/gif）、文档 20MB（pdf/office/txt/md/csv/json/yaml/log）；白名单外类型显式拒绝。上传即新行（id 唯一标识一份内容），version 预留给同业务槽位原位替换递增。

**错误语义**：上传（类型/大小/空文件）、受控访问（found=false/none）、下载（403 撤权）分阶段显式报错；`mode=oss` 四项凭据缺一启动即失败，不回退假成功。

**证据（2026-10-07）**：CRM `service_dev_ai@881878e`（= origin/master `0a5dd8e` + 附件组件提交）；Flyway **V049** 已在 dev 库执行并登记（checksum 362394189；同轮补应用了 V048）；端到端烟测 **16 场景通过**（上传登记/元数据/下载/AI HMAC 访问/inline 字节/跨用户归一 found=false/撤权后下载 403 + access.type=none/不存在归一/错误密钥 401）；AI 侧 `tests/test_p5_file_contract.py` 8 项 + P4 契约回归 5 项 = 13 passed。**不含**：真实 OSS 上传（开放项 B-4）、ASR/OCR 供应商调用、生产 H5。

**仍开放**：OSS bucket 凭据与前缀/签名时效策略（环境类，由用户部署测试环境时解决，技术口径见本节与测试规范 §4）；resource_info 保留期与物理清理任务已由 V050+`ResourceRetentionJob` 实现（仅清已撤权附件，政策确认前默认关闭）；生产 H5 真机验收（H5 形态已确认，见需求 11.5）。

## 13. P4 双端接口契约（由 2026-10-01 冻结稿归并）

### 13.1 事实查询与认证

- AI → CRM：`GET /api/v1/salescrm/ai/integration/facts/{subjectType}/{subjectId}`；type 为 customer/lead/opportunity。Python `HttpCrmFactsClient` 的 `crm.base_url` 必须包含应用前缀。
- 返回 `subject_type, subject_id, found, facts`；不存在归一 `found=false, facts={}`；未知类型/空 ID 为 400，签名或停用身份为 401，数据范围不足保留 403。旧稿“不区分不存在与无权”的表述与此冲突，删除该断言；是否统一防枚举错误需双方另行批准，不擅改现有契约。
- 事实白名单：customer 为 id/name/customer_code/customer_type/value_level/master_status/industry；lead 为 id/lead_code/contact_name/biz_status/owner_id/owner_name/biz_line_name/region_name/next_action；opportunity 为 id/opp_code/name/customer_id/customer_name/stage_name/process_status/close_result/owner_name/expected_close_on。
- 服务端点免用户 JWT 但必须 HMAC 验签，不是匿名接口。CRM 通过 `StaffService.requireActiveById` 查真实员工状态与角色，不信任调用方角色声明；当前数据范围按 CRM 规则 R3/R4/R5 unrestricted、R1/R2 本人创建/经营关系/团队客户，未来变更仍由 CRM 权限实现定义。
- HMAC 串：以换行连接 `key_id、METHOD大写、完整URI path（不含query）、unix秒timestamp、nonce、operator_user_id、body_sha256_hex`；空 body 按空串 SHA256，算法 HmacSHA256 hex。头为 `X-SAI-Auth: SAI-HMAC-SHA256` 及 `X-SAI-Key-Id/Timestamp/Nonce/Signature/User-Id/User-Name`；姓名百分号编码，不作授权依据。
- 时间窗 ±300s。Python 以 `request.url.path`、Java 以 `getRequestURI()` 为签名 path，均含应用前缀；AI nonce 持久化，CRM 当前进程内，集群防重放尚需补齐。新文件授权等接口不能把安全关键参数放在未签名 query 中；若扩展签名规范必须双端版本化和补契约测试。

### 13.2 Run、候选与正式写入

| 步骤 | 当前接口/约定 |
|---|---|
| CRM → AI 创建 | `POST /api/v1/runs`；稳定客户端幂等键（AI 至少 8 位），新生成用新键，弱网重试用原键。 |
| 查询/补参 | `GET /api/v1/runs/{id}`、`GET /api/v1/runs/{id}/pending`；后者含 questions/state_version；`POST /api/v1/runs/{id}/resume` 提交 state_version + values，版本冲突 409，关闭/过期拒绝。 |
| 导入 | CRM 服务端拉取可信 Run 结果，`importRunResult` 重复导入返回已有候选；当前唯一键 `(ai_run_id,item_id)`，目标来源/结果版本与批次约定按第 5 节补齐。 |
| 确认/重试 | CRM 集成路径下 `/runs/{runId}/confirm`、`/runs/{runId}/retry`；现有 items 是 override，**不是已实现明确勾选集合**。上线前必须补选中项、候选/来源版本和确认快照；原键重试先鉴权。 |
| 任务 | `TaskService.createFromExtract` 写 work_tasks，source=EXTRACT，幂等键 `ai:{run_id}:{item_id}`；customer_id/ai_candidate_id 审计关联。本人 OPEN，指派他人 PENDING_ACCEPT，正式 canManageRelation 权限；停止写 sales_task。 |
| 活动 | sales_activity，source=AGENT_CANDIDATE，同类幂等键。当前集成服务事务内 Mapper 写入，尚未收敛 ActivityService；不把现状当最终设计。 |
| 建档 | draft@1 输出候选，CRM importDraftResult/confirmDraft 分派正式客户/联系人/线索/商机 Service；本人确认、查重与权限不可省略，真实端到端仍待验收。 |

契约测试 `tests/test_p4_contract.py`；服务签名验证通过不代表 JWT 用户候选确认、正式写入和权限恢复已通过。迁移与测试记录统一放实施计划，不在本需求重复记“完成率”。
