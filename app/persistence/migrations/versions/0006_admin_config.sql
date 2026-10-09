-- 配置后台落库：连接与账号。
--
-- 后台可配置的全部内容（模型服务 / 外部接口 / MCP 连接、账号与权限）统一存 PG，
-- 不再写 conf/config.ui.yml、conf/secrets.ui.yml、conf/admin.users.yml。
-- config.yml 只保留运行必需、后台不管的项（database_url / auth / api / worker / ocr / asr / oss）。

CREATE TABLE IF NOT EXISTS admin_connections (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('model', 'external', 'mcp')),
    target       TEXT NOT NULL,
    name         TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT false,
    -- 凭据只写不回显：API 响应里永远只有 secret_configured 布尔值
    secret_path  TEXT NOT NULL DEFAULT '',
    credential   TEXT NOT NULL DEFAULT '' ,
    -- 业务字段（provider/base_url/model/url/tool_name/...），按 kind 取用
    config       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS admin_connections_kind_idx
    ON admin_connections (kind, target);

-- 启用互斥：同一 (kind, target) 只允许一条 enabled=true，由数据库兜底（应用层先关旧的）
CREATE UNIQUE INDEX IF NOT EXISTS admin_connections_enabled_uidx
    ON admin_connections (kind, target) WHERE enabled;

CREATE TABLE IF NOT EXISTS admin_users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
    -- pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
    password_hash TEXT NOT NULL,
    -- 改密 / 重置 / 停用后自增，使已签发 Token 立即失效
    token_version INTEGER NOT NULL DEFAULT 1,
    disabled      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);
