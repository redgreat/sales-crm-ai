# CRM AI 实施计划（Python / AgentZR 模板）

> V2.4｜2026-10-02｜依据：[需求](AI接入需求文档.md)。六份阶段文档已归并到两份主文档：本计划保存实施/验收、联调操作及历史证据；需求第 11～13 节保存移动端决策、增强输入流程及双端契约。
> 本次仅归并文档，不重跑云服务、数据库或正式业务写入测试；下列历史通过数字均保留原日期与验证范围。另一智能体正在进行的 ASR/OCR 联调，以其后续回填结果为准。
> `[x]` 仅表示该小项有对应实现/证据；`[ ]` 表示部分实现或验收未闭合。历史完成记录不等于本轮实测，缺失交接文档不作为唯一证据。
> 进度总览：**P0 部分完成 → P1 主体已实现（历史验收）→ P2 主体已实现但恢复门槛待补 → P3 大部分闭环（2026-10-01 消息窗口/轮次幂等/生命周期一致性收口；跨轮指代与真实弱网样本开放）→ P4 进行中（双端契约/权限/一致性阻塞）→ P5 进行中 → P6 未完成**。P-UI 为 Svelte 联调端，不等于生产移动端；P-DEV 保留原验收记录，本轮未复测。
> CRM Java 工作区已存在集成 Controller、HMAC、候选和活动/任务写入实现，不能再标记“未改动”。移动端新增对齐工作包 P-MOBILE，PRD 冲突待评审。

## 1. 路线与门槛

P0 基线 → P1 裁剪骨架 → P2 可靠执行 → P3 最小多轮 → P4 CRM 文字闭环 → P5 全业务能力 → P6 全量验收。P1 后可并行做 P-UI 页面和 P-DEV 启动脚本，但其完成不代替生产闭环。先复用成熟图与 checkpoint，再编写业务节点；不自研 LangGraph 替代品。

## 2. P0：基线、接口与模板清单

- [ ] 补齐可复核的 AgentZR 逐文件复用/许可证/改造清单。（原记录称已完成，但所引 `模板复用清单.md` 当前不存在；来源提交保留在需求，不复述未经核验的模板结论。）
- [x] 核查本轮 CRM/移动端与 AI 相关 PRD，更新代码依赖与差异清单。（见本计划第 12 节及需求第 11 节；Java 已有活动/任务最小实体及写入，不能再沿用“连表都没有”；其完整生命周期及真实环境仍待 P4 验收。）
- [ ] 补齐 AI/CRM/H5-PC 三端可复核的分支、提交与工作区基线；本轮记录 AI/CRM HEAD 和未提交改动，生产 H5 仓库仍待确认。旧分支退役历史见第 11 节，不替代本次跨仓库版本矩阵。
- [x] 锁定 Python、LangGraph/LangChain/checkpointer/数据库驱动版本，实测 Windows 兼容；模板的 >= 依赖不能直接当可复现锁文件。（`requirements.lock`：langgraph 1.2.11 / checkpoint-postgres 3.1.2 / psycopg 3.3.4 / fastapi 0.141.1，Python 3.12.9 实测）
- [x] 双端已具备签名/Run 代理/候选导入接口代码。（AI auth/routes；CRM AiRunClient、AiIntegrationController、AiServiceSignatureFilter；代码存在，不等于联调通过。）
- [x] 事实查询方法/路径/白名单/HMAC 及基础错误契约已于 2026-10-01 冻结并有首轮实测（需求第 13 节）；09-29 的 GET/POST 不一致已解决。
- [ ] 候选导入/确认的选中项、来源/结果版本、审计与 CRM 集群防重放仍需收口，不以事实接口完成代替全部 P0 契约完成。
- [x] 建立并本次复测纯图缺参、多任务、恢复版本、错误输出和问答引用/无依据测试。（命令与去重数量见第 12 节。）
- [ ] 补齐真实有权同名匹配、CRM 来源撤权、长等待及正式提交后响应丢失样本。已有 checkpoint 崩溃恢复用例不等于正式任务写入后崩溃恢复；直接改库标 INVALID 不等于撤权链路验收。

验收：来源清单、契约、版本锁定方案和业务缺口明确。没有凭据可做后续 Stub 契约，不能标记真实接入完成。

## 3. P1：最小 Python 项目与彻底去网关 ✅

目标目录建议：`app/api`、`app/graphs`、`app/runtime`、`app/providers`、`app/integrations/crm`、`app/knowledge`、`app/persistence`、`tests`、`scripts`、`docs`。来源保留记录，不照搬多服务拓扑。（已按此落地；`app/knowledge` 留待 P5）

- [x] 建立 FastAPI API + 同代码库 worker，明确健康/就绪、配置、错误和应用生命周期。（`/health` 与 `/ready` 分离；配置 conf/config.yml；统一错误码）
- [x] 选择性迁入框架状态/持久化封装和有效测试；实现直连供应商模型 Provider，不请求 Open Gateway。（psycopg3 checkpoint 封装改造自模板；ChatOpenAI 直连 + 显式 Stub）
- [x] 移除网关注册/Key 签发/通道、usage/cost 回调、平台模型表和环境变量；过滤供应商 usage 元数据，不落消息/checkpoint/日志。（契约测试扫描 import 无网关/计费符号；strip_usage_metadata 递归剥离）
- [x] 新库最小迁移及 checkpoint 显式初始化入口；启动只检查，不自动改库。禁止执行 AgentZR 全量平台初始化 SQL。（`scripts/init_db.py --apply`、`scripts/init_checkpoints.py --apply`；启动只读校验，缺结构 /ready 503）
- [x] 契约测试验证无网关服务和无网关表时可以启动、调用测试 Provider；Stub 显式启用且生产拒绝。

验收：能独立运行最小健康 API 与单轮测试图，无 AgentZR 运行时路径或 gateway 依赖。不得仅删除 compose 服务后声称去网关完成。✅（52 项测试含真实 PG；**真实 Provider 已于 2026-09-29 用 LongCat 实测通过**，原「无凭据」限制解除）

## 4. P2：可靠执行底座（主体已有，仍有验收缺口）

- [x] PostgreSQL 持久 Run、请求幂等、输入版本、队列扫描及有限重试；认领/续租/超时使用执行代次保护，旧执行者不能提交。（FOR UPDATE SKIP LOCKED 认领；execution_generation 守卫；真实库测试）
- [ ] checkpoint 持久化主体已有；补全旧状态升级或明确阻断策略及跨版本测试，不以“当前无历史”勾选完整完成。
- [x] 模型节点输出持久化与阶段提交，重复调度不覆盖成功结果；跨存储边界通过幂等、查询和对账恢复，不假装跨库原子。（complete_run 代次守卫；resume 崩溃后从 checkpoint 状态补落结果）
- [ ] 已有租约接管、取消、重试等数据库测试源码；真实进程中断、队列饱和和部署恢复仍需实测。租约回收模拟不能标为等价通过全部故障矩阵；本轮未启动共享临时 PG。
- [x] 运行、图等待、候选和正式写入状态分开；模型调用不持长事务，故障不降级无状态图。（queued/running/waiting_input/succeeded/failed/cancelled；interrupt 后释放租约）

验收目标：重启后继续可恢复阶段，旧执行者隔离，任务不丢；补齐故障矩阵与运行手册。原计划曾记录真实 PG 重启通过，本轮未复测且所引交接文件缺失，不作为全部恢复门槛通过的证明。

## 5. P3：会话、短期记忆、追问和恢复（部分完成）

- [x] 已建立会话/消息存储、owner/对象绑定、thread 映射和结构化补参状态。（conversations repo、runs 路由、graph nodes/interrupts；本次图测试通过。）
- [x] 消息幂等及完整轮次窗口：最近窗口替代「最早 100 条」；每条消息独立 Run（修复固定 `conv:{id}:{user}` 键导致第二条消息重放第一个 Run 的缺陷）；客户端同 key 重试幂等重放原 Run 且消息不重复落库（迁移 0004 `client_key` 唯一索引；`GET /messages` 支持 `limit`/`before_seq`）。**（2026-10-01：`tests/test_p3_conversations.py` 10 passed）**
- [ ] 图原生 interrupt/resume 和专门 resume 路由版本守卫已有；补齐所有入口并发/关闭态验证，不能仅凭专门路由测试整体完成。（**2026-10-02**：messages 入口支持可选 state_version——携带时严格校验（与 /runs/{id}/resume 同语义，防旧页面恢复新等待），不携带视为对话式补参；`test_message_resume_with_wrong_state_version_rejected` 通过。）
- [x] 支持缺人员/日期的追问、用户补充、最终候选确认；每轮独立 Run，不把对话补参等同正式创建授权。（候选仅建议输出，正式写入永远在 CRM 侧）
- [x] 对象问答图已支持历史上下文与合法引用分离；本次验证历史不能作为事实引用。（`qa.py`、`test_object_qa_answer_cites_facts_not_history`。）
- [ ] 完整连续追问验收：最近完整轮次窗口/必要摘要、每轮当前事实权限、跨轮指代、旧消息和 checkpoint 撤权。历史标 `[H]` 不等于获当前授权。
- [x] 清空/结束/过期、切换对象、历史及知识撤权同步处理；旧 checkpoint 不得恢复无权材料进入模型。（**2026-10-01 闭合**：结束✅、切换对象=新建会话✅、TTL 过期✅——迁移 0004 `expired` 状态 + `conversations.ttl_hours` 默认 72h + worker 低频扫描，M-01 红线「会话过期不改候选状态」已测；closed/expired 在 post_message / create_run / resume 三入口一致拒绝✅；历史撤权（M-10 `filter_unauthorized_history`）✅。checkpoint 撤权经复核**无暴露面**：qa 族持有知识/历史但无 interrupt 节点（每轮重新装配+过滤），extract 图会 interrupt 但 checkpoint 不含知识/历史材料；qa 族将来若加 interrupt，须在 resume 前对 checkpoint knowledge_facts 按当前授权重过滤）
- [ ] 已有重启续问/重复 resume/owner 检查用例；历史撤权、状态跨版本和所有入口一致性仍待真实验收。本轮只复测纯图，不重新认证历史 PG/模型结果。（**2026-10-01 部分收敛**：closed/expired 全入口生命周期一致性已由 `test_p3_conversations.py` 覆盖；跨轮指代与真实弱网/并发样本仍开放）

验收：结构化补参主体已落地，P3 整体仍未通过。原计划记录的 LongCat/RDS 属历史描述，当前缺少所引交接文件；即使模型可调用，也不能替代历史撤权、完整窗口和真实 CRM 权限验收。

## 6. P4：CRM 直接集成与文字闭环（进行中，真实闭环未验收）

- [x] CRM 集成骨架已存在：Run/pending/resume 代理、候选导入/列表/确认/忽略/重试、服务签名、活动/任务实体及 Mapper。（已随 9598222/8c82dd7 提交进 service_dev_ai 分支。）
- [x] V010 迁移版本冲突已解决（2026-10-01，用户授权 MCP 直接操作 zrcrm 库）：分支快进 origin/master 取回 V014-V017；冲突脚本定版 `V018__ai_agent_candidates.sql`（`confirmed_by`→`confirmed_by_id` 对齐实体，全幂等）；新增 `V019__work_tasks_extract.sql`。两版本均已在 zrcrm 库执行并登记 flyway history（rank 19/20，checksum 按 LineChecksum 算法核对，且 validate-on-migrate=false）。
- [x] 双端事实查询契约已冻结（2026-10-01，见 需求第 13 节）：Java 新增 `GET /ai/integration/facts/{subjectType}/{subjectId}`（customer/lead/opportunity），删除旧 POST customer 专用端点；**修复 Python 签名 path 口径缺陷**（原签相对路径，Java 验签用完整 getRequestURI，必 401——现从 base_url 提取应用前缀）；`tests/test_p4_contract.py` 5 passed（MockTransport 验证方法/路径/签名重构/响应解析/401 透传）。
- [x] 任务候选确认写入已切换正式 Service（2026-10-01）：`TaskService.createFromExtract` → **work_tasks 统一任务表**（source=EXTRACT，V019 加幂等键/客户锚点/候选关联三列），本人确认=OPEN、指派他人=PENDING_ACCEPT（正式指派权限 canManageRelation，弃用 AI 专用 R1/R2 判定），并发确认由幂等唯一索引兜底；`sales_task` 停止写入。
- [ ] CRM Java 仅增加集成薄层：认证上下文、事实查询、Run 代理/结果拉取、候选导入；不再实现模型编排或记忆。（骨架符合；活动写入仍由集成服务事务内 Mapper 直插——CRM 无独立 ActivityService，活动管理入口属后续迭代，见需求第 13.2 节。）
- [x] 冻结服务认证和最小用户授权凭据；Python 不直连 CRM 库，CRM 不暴露模型 Key。（签名方案双端逐字段一致；密钥经环境变量注入。）
- [ ] 沟通/会议产生建议 → CRM 幂等导入候选 → 用户补齐/确认 → 正式活动/任务 Service；真实业务接口缺失显式补在 CRM，不造 Python 正式任务表。（任务侧闭环已具备；活动侧待 ActivityService 收敛。）
- [x] 顺序重复导入查既有候选、先活动后任务及按原业务键查正式对象的基础代码已有。（不等于并发和故障恢复通过。）
- [ ] 补齐稳定客户端请求键、来源/结果/候选版本、明确勾选项集合、确认快照、员工和客户数据权限；正式写入复用完整业务 Service，回执/审计一致性与响应丢失对账通过。（**2026-10-02 三项已落地（CRM service_dev_ai，未提交待用户指示）**：①稳定客户端请求键——createRun 支持 client_request_key，未传按员工+能力+内容+当日派生，弱网重复点击命中同一 Run；②客户同名匹配走数据范围——非 R3/R4/R5 限本人创建的客户（消除无权客户 id/存在性泄露），经营关系客户由写入侧 checkCustomerDuplicates 带范围查重兜底；③confirmRun 支持显式 item_ids 勾选集合（M-02 不静默整批确认，空集合非法，兼容旧整批调用）。**仍开放**：来源/结果/候选版本快照、审计一致性。`tests/test_p4_draft_live.py` 3 passed 回归。）
- [x] **真实联调首轮通过（2026-10-01，不需要 CRM JWT 的 HMAC 事实链路）**：CRM 应用本地启动（service_dev_ai@af2d6c3，dev 配置连真实 zrcrm 库，Flyway V018/V019 校验通过），`scripts/crm_facts_live_check.py` **9 passed**——真实签名、真实客户/线索/商机数据、数据范围 403、404 归一 found=false、未知类型 400、错密钥/nonce 重放 401。联调暴露并修复三处：StaffIdentityFilter 未豁免 integration 路径、HMAC 链路角色固定空数组/停用不拦（改为从 staff 表读真实身份）、对象不存在未归一 found=false。
- [ ] 验证「正式提交成功但响应丢失」、重复确认、候选版本改变、无指派权限、来源撤权和跨用户查询。（候选导入/确认链路需 CRM JWT 用户身份，待用户提供工号凭据后执行 `tests/test_crm_integration.py --run-crm`。）

验收：真实文字生成到正式业务写入闭环；Stub/模拟确认不算。记录涉及 CRM 仓库的分支、变更文件和联调版本，不整体合并旧 AI 分支。

本轮证据约束：已有历史全套日志记录 CRM 7 项失败；Java JDBC 测试的若干用例直接改库而未调用业务服务。必须补真实 Controller/Service 的撤权/版本/越权测试，并断言合法样本至少成功写入活动和任务，不能接受“全部业务写入失败也算全链路通过”。

## 7. P5：全业务能力与增强输入（进行中）

### 7.1 已有能力与证据边界

- [x] 4.4/4.5/4.10/4.11：日报、对象摘要、今日任务、主管关注；`app/graphs/analysis.py`、预生成幂等；2026-09-29 `test_p5_analysis.py` 记录 12 passed。CRM 负责三项定时预生成、正式事实与日报版本。
- [x] 4.6～4.9：对象/跨对象/知识问答、会前准备；`qa.py`、授权先于模型、无依据降级。09-30 补知识相关度排序，`test_p5_knowledge.py` 记录 13 passed，不等于企业样本检索质量验收。
- [x] 4.16：知识发布/停用同步及授权索引，迁移 0002；09-30 迁移 0003 增 is_current、组织维度 scope 拒绝、撤权历史过滤。日报 as_of/facts_count、fact_guard 与能力目录同时补齐；`test_mobile_prd_alignment.py` 记录 17 passed。
- [x] 4.14：纯文本 txt/md/log/csv/json/yaml 解析，`app/integrations/files.py`、`POST /api/v1/files/parse`；逐项失败、编码回退、截断标记。09-30 `test_p5_file_parsing.py` 记录 16 passed（旧计划“14 项”不再沿用）。
- [ ] docx/pdf/xlsx/扫描页及页码证据未因纯文本解析完成而验收；当前不支持格式必须显式报错。
- [x] 4.12/4.13 ASR/OCR 适配层及供应商 POC：2026-10-02 原联调记录为 OCR 合成中文图 8 区域带坐标、ASR 官方公网单人音频三步识别及时间锚点通过；仅代表这些样本，不代表本地上传、真实多人音频、LLM 或正式 H5 闭环完成。
- [ ] 4.15 大圆：尚未确认可用对外接口，不把企业微信其他付费/灰度功能当作同一接口；保留手动授权文本输入降级。

### 7.2 P5-INPUT：上传 → 识别 → LLM → 确认（新增实施包）

按需求第 12 节推进，**保留 AI 侧现有 ASR/OCR/OSS 适配器，不在 Java 重复引入识别 SDK**。另一智能体继续做供应商 POC；下面各项是尚未验收的生产链路，不要求重写其正在测试的实现。

1. **P4 + CRM 文件边界**
   - [ ] 盘点 CRM 文件服务，冻结 fileId/sourceVersion/处理类型、上传授权/完成校验、受控读取、撤权与保留期；明确 H5 直传或现有上传接口，不重复搬运文件。
   - [ ] 生产 H5 经 CRM 登录和 Run 代理；file_ref 权限由 CRM 校验并由 AI 在使用前取得受控访问，不直接采用 playground 测试身份。
2. **AI 增强输入入口与持久阶段**
   - [ ] 支持 text/file_ref 两类输入，校验格式、大小、时长、来源版本；复用现有 Run/worker/LangGraph，不另造队列。
   - [ ] 识别输出、供应商 task_id、来源锚点与配置版本持久化；重启可查原任务/复用结果，重试 LLM 不重做识别；明确取消和迟到结果隔离。
   - [ ] LLM 整理输出与原始识别文本独立版本；数字、日期、姓名不静默改写，无证据不补事实；识别原文在整理失败时仍可返回。
   - [ ] 上传/识别/整理/正式写入分阶段错误；短时链接过期重新鉴权，URL 白名单/内网与重定向防护、下载上限、日志脱敏和临时文件清理。
3. **H5/联调端**
   - [ ] 共用上传、进度、离页恢复、原文/优化文本对照、锚点定位、人工编辑和阶段重试；M16 语音只进入日报草稿，不自动提交。
   - [ ] 正式候选仍由 CRM 导入、本人确认后写业务；文件或候选版本改变阻止旧结果写入，弱网复用原请求键。
4. **分层验收**
   - [ ] 私有 OSS 本地文件上传 → 受控签名读取 → ASR，以及真实图片 OCR，各有安全测试样本与回执。
   - [ ] ASR/OCR → LLM → 展示/编辑 → CRM 候选确认真实闭环；校验原文、优化文、用户最终文本三层及原件关联。
   - [ ] 凭据错误、链接过期/撤权、无权文件、模糊图、超限、供应商超时、LLM 失败、阶段崩溃、取消迟到、重复请求、部分业务写入及响应丢失。
   - [ ] 真实多人/中文专名/金额日期样本校验，说话人只标号不认员工；误识别如实展示并允许校对，不用单个样本“全正确”推定质量合格。

顺序：复用当前 POC → 冻结文件契约 → AI 阶段编排/结果版本 → H5 展示确认 → P4 正式闭环/故障验收。P6 必须等待本包与 P-MOBILE 验收，不能以供应商 API 单独成功替代。

### 7.3 ASR/OCR 联调操作（原手册有效部分）

仅在明确测试环境、授权样本和允许调用付费云服务的前提下执行；本次文档整理不执行以下命令。密钥只放环境变量或忽略的本地配置，不写入文档、测试输出或 Git。

| 能力 | 配置及开关 |
|---|---|
| ASR | `SAI_ASR_API_KEY`；`asr.enabled`。当前 DashScope 录音文件适配器使用可访问 URL，提交 → 轮询 task_id → 下载 transcription_url。 |
| OCR | `SAI_OCR_ACCESS_KEY_ID` / `SAI_OCR_ACCESS_KEY_SECRET`；`ocr.enabled`。当前 RecognizeAllText 适配器支持 URL 或图片二进制，不强依赖 OSS。 |
| OSS | `SAI_OSS_ENDPOINT` / `SAI_OSS_BUCKET` / `SAI_OSS_ACCESS_KEY_ID` / `SAI_OSS_ACCESS_KEY_SECRET`；`oss.enabled`。私有 Bucket，限定对象前缀及所需读写权限，签名时效按实际异步拉取窗口验证。 |

环境变量优先于本地配置；启用而缺凭据应明确失败，不回退假成功。09-29 默认关闭描述是样例默认值，10-02 历史 POC 已获得 ASR/OCR 凭据，不再列“待申请”为当前阻塞；OSS 实际配置/最新进度由正在联调的智能体回填。不迁移旧手册的价格/免费额度及未验证的 RAM 策略名。

在项目根目录按样本和配置检查脚本后运行：

```powershell
.\.venv\Scripts\python.exe scripts/ocr_poc.py
.\.venv\Scripts\python.exe scripts/asr_poc.py
```

OCR 看区域锚点、数字/日期及模糊/大图错误；ASR 看时间锚点、说话人、超时与失败，官方单人样例不能验证真实说话人分离。本地录音没有受控可访问来源时应明确失败，不绕过上传权限。逐次回填日期、提交/工作区状态、脱敏样本标识、命令、实际断言、失败/限制；结果记本节，不再新增日期型手册。

历史修复备忘：OCR RuntimeOptions 导入改为 tea_util，二进制 SDK 调用用同步方法 + asyncio.to_thread，Tea 响应 to_map 并解析真实 block_details/block_points；相关修复已由 10-02 POC 记录，不重复当作待开发项。

## 8. P-UI：AI 联调前端 ✅（替代实现）

> 2026-09-29 决策：联调前端不再迁入 sales-crm-admin-ui（Vue2），改为本项目 `frontend/` 独立 Svelte 应用（SvelteKit + Tailwind v4 + shadcn-svelte lyra 风格：暗黑主题、无圆角）。CRM Vue2 内的最终业务入口仍留待 P4 后按需求第 6 节处理。

- [x] 独立路由/目录，复用现有登录、请求封装及权限，不引入无关组件。（本项目独立前端；认证经 `/playground/api` 开发代理在服务端签名注入联调身份，浏览器不持密钥；仅 dev/test 且配置显式开启，生产 404 且拒绝启动）
- [x] 能力选择、样例输入、Run/会话状态、结果/引用、追问、resume、取消和失败重试。（三个 Tab 的历史验收记录保留。）
- [ ] 上传、识别/整理进度和原文/优化文本对照按 P5-INPUT 验收；不得随核心三个 Tab 整体勾选。
- [x] 显示 Stub/真实 Provider/真实 CRM 模式与版本；正式测试写入必须显式确认。（模式徽章=开发联调/Stub；无任何正式写入路径；真实 CRM 模式待 P4）
- [x] 前后端共同限制开发测试环境及技术角色；验证不绕过 CRM 数据权限。生产默认关闭测试功能。（生产配置拒绝开启；跨用户 403 在 API 层测试）
- [x] 浏览器验收：生成、追问、刷新恢复、跨用户拒绝、部分失败、引用点击、真实接口请求匹配展示。（**已完成**：Stub 与真实模型（LongCat）的生成/追问/补参/取消/引用浏览器截图验收✅；刷新恢复、跨用户拒绝、部分失败的浏览器级用例已补——`scripts/browser_test.py` 3 passed）

## 9. P-DEV：新仓库一键启动前后端（保留历史验收，本次未复测）

- [x] `scripts/dev.ps1 start/status/stop`，路径/端口可配；默认 Python API/worker + 前端，CRM 后端地址外配，显式参数才启动本地 Java 后端。（配置读 conf/config.yml；`-NoUi` 跳过前端）
- [x] 预检 venv/锁定依赖、Node/包管理器、端口、数据库版本、CRM 健康；不自动执行数据库迁移或全局安装。（缺结构时给出显式脚本提示，绝不自动改库）
- [x] 后台隐藏启动、PID/启动时间归属、就绪等待、日志脱敏；失败清理本次子进程，重复 start 幂等，stop 不杀其他服务。
- [x] 配置样例只放占位符；本地密钥、PID、日志、venv 等加入忽略。默认 localhost，代理/CORS 按契约最小开放。（conf/config.yml.example；vite 仅代理 /playground）
- [x] 实测全冷启动、已有进程、端口冲突、依赖缺失、服务不就绪、重复启动与停止；输出健康地址和 AI 页面入口。（**已完成**：`scripts/fault_matrix_test.py` 4 passed——端口冲突、依赖缺失、服务不就绪、配置缺失）

验收：一条命令拉起所选服务并打开可用入口；脚本存在但未运行不能标记完成。资源和凭据不足明确报告。（启动入口已实测：API+前端+playground 浏览器闭环）

## 10. P6：验收、发布和交接（未开始）

新增依赖：P-MOBILE 的冲突结论和关键端到端用例必须收口，Svelte 测试页不能替代企业微信 H5 验收。P0/P3/P4 本轮缺口见第 12 节，先补授权/一致性再扩入口。

- [ ] 锁文件可重建、单元/契约/真实 PostgreSQL/浏览器/真实模型与 CRM 测试分开记录。
- [x] 扫描代码/部署/迁移/页面：无网关运行依赖、usage 计费表、平台默认账号及模板秘密；无生产 Stub。（**已完成**：`tests/test_p6_scan.py` 5 passed——无网关依赖、无计费持久化、无硬编码密钥、生产拒绝 Stub、无默认账号）
- [ ] 性能、并发、上下文上限、队列积压、恢复、权限撤销、密钥轮换和手工降级通过。
- [ ] 交付初始化/迁移/启动/回退说明和跨仓库版本矩阵。回退不删除正式任务，不用清空 checkpoint 掩盖故障。

按门槛排期，不承诺旧 Java 八周排期覆盖重建。P0 盘点后估工；每步交接列完成项、测试证据、缺口及下一步，不提前勾选。

## 10A. P-MOBILE：移动端 PRD 对齐与接入（AI 侧部分已实现，生产接入待验收）

依据需求第 11 节及第 11.5 节决策台账。V2.6 已确认事项按最新口径实施，未决批量范围/知识组合权限等不擅自批准；接口代码完成不等于生产 H5 完成。

- [x] 对当前 DEV_SPEC 28 页逐项检查，结合后端 04/05/08 登记 AI 入口、冲突、缺项和建议；完成文档快照，不代表页面已修改。
- [x] 产品/移动端负责人确定候选生命周期、批量及追踪任务、对话建档、工作手机录音、消息偏好、知识分享等 M 项；回填决定和版本。（**2026-10-01 复核产品版 V2.6（2026-09-30）**：M-01/02/04/05/06/07/08/09/11/12 已按后端口径确认；**M-03 用户确认必须实现，已于 2026-10-01 落地**（新增类 4 项：AI 侧 draft@1 图+注册表+目录透出，CRM 侧 V025+导入/确认写正式 Service；查询类 2 项映射现有 qa 族）；M-10 权限组合语义待定（AI 侧纵深防御不受影响）；遗留待确认见 需求第 11.5 节：M03 批量确认是否限同会议来源、知识权限组合语义、「丢弃无痕」措辞、生产 H5 仓库）
- [ ] 确认实际 H5 代码仓库、企业微信登录、staff 映射及 CRM 代理契约；保留 PC 轻量入口，独立 Svelte 只作 dev/test 联调。
- [ ] P4 冻结 page-id/capability/来源与对象版本/Run/会话/候选及 API 映射；稳定客户端请求键、选中项与版本成为确认契约，不静默按整批确认。
- [ ] P3 补齐关闭/过期/窗口记忆、所有 resume 入口版本校验与历史撤权；移动端实现收起/离页/刷新恢复、对象切换和无证据/缺参/失败状态。
- [ ] 接入 M03/M11/M15/M25 文字及候选核心链，先活动后逐项任务、明确逐项结果；录音/图片/文件按专项 POC 启用，排除工作手机自动采集。
- [ ] 接入 M01/M09/M16/M17～M19 配套能力，日报三层版本、只读引用、当前权限及固定三项预生成，不新增 AI 自动推送。
- [ ] 真 H5 环境验证弱网重复请求、跨用户深链、同名匹配、版本冲突、部分成功、响应丢失恢复、日报刷新保护、知识撤权及手工降级，关联后端 PRD 用例。

- [x] **M-03 对话建档（产品版 V2.6 一期 6 项能力）实现（2026-10-01）**：新增类 4 项——AI 侧 `draft@1` 图（`app/graphs/draft.py`：按类型整理字段→必填缺失追问→草稿输出，图内零写入路径）+ 注册表 4 能力（customer/contact/lead/opportunity.draft，目录自动透出）+ `tests/test_p_mobile_drafts.py` 6 passed；CRM 侧 V025（candidate_type 扩展+draft_payload，避让用户侧 V020-V024 改号）+ `importDraftResult`（同名匹配，先匹配再新增）+ `confirmDraft`（分派 Customer/Contact/Lead/Opportunity 正式 Service 写入，重复确认幂等）。查询类 2 项映射现有 qa 族（object.qa / knowledge.qa+business.qa），不重复建设。端到端联调 `tests/test_p4_draft_live.py` 待 CRM JWT。全量回归 158 passed。

完成条件：阻断 M 项已决定并回写移动端原文；生产 H5 + CRM + AI 同一版本完成真实测试。未决扩范围不默认实现，当前新增清单不承诺旧排期可覆盖。

## 11. 旧分支退役

旧 CRM 本地分支 `ai-h5-sandbox` 有未合入 master 的提交且混有非 AI 变更。删除前保留归档标签和提交号，切回 master 后仅删除指定本地分支，保留未跟踪工作区文件。远端分支不默认删除；不执行 reset --hard、不删除工作目录，不把旧分支整体合进主线。后续 CRM 薄层集成从最新主线按独立任务实现。

2026-09-28 已执行本地退役：归档标签 `archive/ai-h5-sandbox-20260928` 指向 `3576f6d3e0ba69f84eb8f81e6c3053f7ec2dd97a`；CRM 已切回 master，本地 ai-h5-sandbox 已删除，远端保留。未跟踪的 `sales-crm-api-service.code-workspace` 保留。需要恢复时，可在 CRM 仓库用 `git branch ai-h5-sandbox archive/ai-h5-sandbox-20260928` 重建分支；不要整体合并归档提交。

## 12. 归并后的证据索引与文档维护

### 12.1 历史证据（不是 2026-10-02 重新实测）

| 日期 | 证据与边界 |
|---|---|
| 09-29 | AI HEAD 0074ebf、CRM HEAD 40366fb，均含其他 Agent 工作区改动。纯图/Provider/选定问答命令 14 passed；analysis+knowledge 排除 realpg 13 passed/11 deselected；两批重复 6 项，合计 **21 个不同用例**。未启动共享测试 PG、未调用真实 CRM 写入。 |
| 09-30 | mobile_prd_alignment 17、file_parsing 16、knowledge 13 passed；原全套记录 141 passed/7 failed，失败为 CRM 连接/鉴权链路，不能未复现就归因 token。修复 is_current、历史知识引用过滤、scope 失败即关闭、fact_guard、能力目录及相关度排序。 |
| 10-01 P3 | test_p3_conversations 10 passed、当时全量 147 passed（未跑既有 CRM 环境依赖）。最近消息窗口、逐轮 Run/client_key 幂等、TTL/三入口关闭过期拒绝、连接池类型引用修复；跨轮指代/版本恢复/真实弱网仍按 P3 开放。 |
| 10-01 P4 | test_p4_contract 5 passed（MockTransport）；crm_facts_live_check 9 passed（真实 zrcrm、CRM service_dev_ai@af2d6c3），涵盖客户/线索/商机、403/404/400、错误签名与 nonce。**不包含 JWT 候选正式确认闭环**。 |
| 10-01 建档 | test_p_mobile_drafts 6 passed、当时全量 158 passed；draft@1 四种草稿能力、CRM V025/importDraftResult/confirmDraft。test_p4_draft_live.py 正由其他智能体维护，不能据源码存在推定真实建档验收。 |
| 10-02 输入 | 原手册记载 OCR 合成中文图 8 区域锚点、ASR 官方公网单人样例通过；OCR 相关单测记载 20 passed。OSS 本地上传、LLM 串联、真 H5/正式业务闭环未在该记录中验收，按第 7 节补测。 |

上述不同日期/范围数量不可相加当作当前全量测试数。最初 ASR/OCR Mock 测试 20 passed 与后续 OCR 测试记录也不可视为不同用例累计。

09-29 纯逻辑复现入口（先检查当前测试 fixture 和环境，旧数量不保证不变）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_graph.py tests/test_providers.py tests/test_p5_knowledge.py::test_qa_registry_contract tests/test_p5_knowledge.py::test_object_qa_answer_cites_facts_not_history tests/test_p5_knowledge.py::test_knowledge_qa_ignores_fabricated_knowledge_citation tests/test_p5_knowledge.py::test_knowledge_qa_without_any_context_discloses_without_model tests/test_p5_knowledge.py::test_business_qa_rejects_over_limit_in_graph tests/test_p5_knowledge.py::test_meeting_prepare_question_derived_from_topic -q
.\.venv\Scripts\python.exe -m pytest tests/test_p5_analysis.py tests/test_p5_knowledge.py -m "not realpg" -q
```

测试安全：历史 conftest 会停止/清理共享 `.local/pg-test`；真实 CRM 测试会读取本地凭据并可能创建正式对象。先确认隔离环境与授权，不与其他智能体共用实例跑破坏性 fixture；不得仅为文档勾选运行全套。

### 12.2 跨仓库迁移与仍开放门槛

- CRM 历史提交 9598222/8c82dd7 为集成骨架，10-01 真实事实测试基线 af2d6c3；AI/H5 当前完整版本矩阵仍由 P0 补齐，不把历史 SHA 当当前 HEAD。
- V010 重号已收敛至 `V018__ai_agent_candidates.sql`（confirmed_by_id）、`V019__work_tasks_extract.sql`（customer_id/ai_candidate_id/idempotency_key 与部分唯一索引）；10-01 记录在 zrcrm 执行并登记 Flyway rank 19/20、核对 LineChecksum。
- 该环境历史设置 validate-on-migrate=false，不是新环境推荐配置或跳过校验的授权。V025 为建档候选扩展（避让既有 V020–V024）；各环境部署须核实脚本与迁移历史，不能照搬手工登记。
- 已解决的旧问题不再作为阻塞：GET/POST 事实接口不一致、V010 重号、最早 100 条历史、同会话固定请求键、生命周期入口遗漏、v9/v10 字符串版本排序。
- **仍开放**：模板复用/许可证清单；跨仓库版本矩阵；全部恢复入口 state_version 与旧 checkpoint 升级；当前权限下完整历史/摘要处理；同名匹配/override 客户权限及对象状态；活动正式 Service；确认勾选集合与候选/来源版本、负责人/活动时间默认值显式确认；审计/正式写入事务边界、响应丢失对账；真实 H5 与增强输入闭环。
- CRM 测试必须走 Controller/Service 并断言合法样本确实创建正式对象且重复请求返回相同 ID；直接改库置 INVALID、只比 JSON 或允许全部写入失败，不算撤权/版本/闭环通过。

### 12.3 文档职责与清理记录

docs 仅维护两份主文档：需求保存“做什么、边界和契约”，实施计划保存“怎么做、进度、验收与联调”。新增需求更新对应章节，不继续累积日期型评审/手册。

2026-10-02 已归并后删除的六份受 Git 跟踪文档：
- 移动端PRD对齐建议-2026-09-29.md、移动端PRD对齐-AI侧落实-2026-09-30.md、移动端PRD-V2.6复核对齐-2026-10-01.md → 需求第 11 节、计划 P3/P-MOBILE/本节。
- P4-双端集成契约.md → 需求第 13 节、计划 P4/12.2。
- ASR-OCR联调手册.md → 需求第 12 节、计划第 7 节。
- 代码进度核验-2026-09-29.md → 各阶段开放项及本节历史证据。

删除的是重复文件，不是取消未完成事项；原文可从 Git 历史恢复。本轮无测试重新验收、无数据库迁移、无云服务调用，不覆盖其他智能体的代码改动。
