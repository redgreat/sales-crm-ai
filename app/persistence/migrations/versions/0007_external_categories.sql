-- 外部接口分类化 + 凭据结构升级。
--
-- 1) credential TEXT → JSONB：一个连接可带多个凭据字段
--    （模型/CRM/MCP 仍是 {"secret": ...}；OCR/OSS 是 access_key_id + access_key_secret，
--    ASR 是 {"api_key": ...}）。旧文本值统一包成 {"secret": 旧值}，语义不变。
-- 2) 删除 secret_path 列：密钥落点由代码按 (kind, target) 决定，不再暴露给后台。

ALTER TABLE admin_connections ALTER COLUMN credential DROP DEFAULT;

ALTER TABLE admin_connections
    ALTER COLUMN credential TYPE JSONB
    USING CASE
        WHEN credential IS NULL OR credential = '' THEN NULL
        ELSE jsonb_build_object('secret', credential)
    END;

ALTER TABLE admin_connections
    ALTER COLUMN credential SET DEFAULT '{}'::jsonb;

ALTER TABLE admin_connections DROP COLUMN IF EXISTS secret_path;

-- 外部接口新增分类（ocr / asr / oss）只是 target 取值扩展，target 本身无约束，无需改表。
