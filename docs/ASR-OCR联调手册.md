# ASR / OCR 联调手册（凭据到位后执行）

> 适配层已就绪（`app/integrations/asr.py`、`ocr.py`、`oss.py`），**默认关闭**。
> 本手册是拿到凭据后的操作清单，按顺序执行即可完成 POC。
> 原则：**未实际跑通不得宣称完成**；缺凭据、缺样本时如实记录为"未验证"。

## 1. 开通与凭据

| 能力 | 需要什么 | 填到哪 |
|---|---|---|
| ASR | 百炼（DashScope）API Key | `SAI_ASR_API_KEY` 或 `conf/config.yml` 的 `asr.api_key` |
| OCR | 阿里云 AccessKey ID + Secret | `SAI_OCR_ACCESS_KEY_ID` / `SAI_OCR_ACCESS_KEY_SECRET` |
| OSS | Bucket + AccessKey（**仅识别本地录音时需要**） | `SAI_OSS_ENDPOINT` / `SAI_OSS_BUCKET` / `SAI_OSS_ACCESS_KEY_ID` / `SAI_OSS_ACCESS_KEY_SECRET` |

注意三点（最容易搞混）：

- **ASR 用 API Key，OCR/OSS 用 AccessKey**，不是同一套凭据。
- **OCR 不强依赖 OSS**（图片可走 `Url` 或二进制 body）；只有 ASR 才必须公网 URL。
- 环境变量优先于配置文件，密钥可完全不落盘。

开启后若凭据缺失，服务**拒绝启动**（`SettingsError`），不会静默降级成"假装识别成功"。

## 2. 开通动作

1. 百炼控制台开通"录音文件识别"→ 创建 API Key。
2. 阿里云控制台完成实名 → 开通"文字识别 OCR"→ 创建 RAM 子账号 AccessKey，
   只授予 `AliyunOCRReadOnlyAccess`（够用即可，不给全权限）。
3. 若要识别本地录音：创建 OSS Bucket（**私有读写**），给子账号授予该 Bucket 的
   `oss:PutObject` 与 `oss:GetObject`。签名 URL 默认 1 小时，足够提交识别任务。

## 3. 联调步骤

### 3.1 OCR（先做，依赖最少）

```bash
# 1) 填入凭据后启动服务
export SAI_OCR_ACCESS_KEY_ID=<...>
export SAI_OCR_ACCESS_KEY_SECRET=<...>
# conf/config.yml: ocr.enabled = true

# 2) 用一张含数字/日期的真实业务图片验证
#    重点看两件事：
#    - 文本是否准确（尤其是金额、日期、百分比）
#    - anchored_text() 是否带出原图锚点 <R1@x1,y1,...>
```

验收点：
- 关键数字/日期准确（需求 4.13 要求这些必须人工确认，识别错会误导）；
- 锚点坐标能定位到原图区域；
- 大图/模糊图返回明确错误，而不是空字符串。

### 3.2 ASR（需公网 URL）

```bash
export SAI_ASR_API_KEY=<...>
# conf/config.yml: asr.enabled = true
# 本地录音 → 还需 oss.enabled = true
```

流程：提交任务（`X-DashScope-Async: enable`）→ 轮询 `task_id` → 下载 `transcription_url`。
适配层已实现异步三步骤与超时/失败处理。

验收点：
- 时间锚点 `[HH:MM:SS]` 与音频实际位置对得上；
- 说话人分离只出 `speaker_id`（"说话人0"），**不映射员工**——这是需求 4.12 的硬要求；
- 轮询超时（默认 900s）显式失败，不返回空结果；
- 未启用 OSS 时上传本地文件必须报错（百炼不支持本地文件/Base64）。

## 4. 联调后必须回填

- [ ] 真实音频/图片各跑通一次，记录样本与结果
- [ ] 锚点、说话人、失败重试逐项验证
- [ ] 把实测结论写进 `docs/AI接入实施计划.md` 第 7 节（4.12/4.13 从"适配层就绪"改为"已验证"）
- [ ] 若识别质量不达标（如中文专名错误率高），如实记录并给出改进方向，不掩盖

## 5. 未覆盖（明确不做）

- 实时会议转写、工作手机后台录音自动采集 —— **不在本期**（移动端 PRD 对齐 M-04）。
- 说话人映射到具体员工 —— 需求明确禁止。
- ASR/OCR 结果直接写入正式业务对象 —— 一律走候选 + 人工确认。
