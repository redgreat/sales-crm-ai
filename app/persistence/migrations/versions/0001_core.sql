-- 0001 核心运行数据：Run/队列、会话、消息、认证 nonce。
-- 本项目专属 schema，不包含任何网关/计费/平台表。

CREATE TABLE ai_runs (
    run_id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    capability TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','waiting_input','succeeded','failed','cancelled')),
    input JSONB NOT NULL,
    input_hash TEXT NOT NULL,
    resume_values JSONB,
    result JSONB,
    result_version INTEGER NOT NULL DEFAULT 0,
    error JSONB,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    execution_generation BIGINT NOT NULL DEFAULT 0,
    thread_id TEXT NOT NULL,
    conversation_id UUID,
    operator JSONB NOT NULL,
    graph_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX idx_runs_queue ON ai_runs (run_after) WHERE status = 'queued';
CREATE INDEX idx_runs_conversation ON ai_runs (conversation_id) WHERE conversation_id IS NOT NULL;
CREATE INDEX idx_runs_thread ON ai_runs (thread_id);

CREATE TABLE ai_conversations (
    conversation_id UUID PRIMARY KEY,
    crm_user_id TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    thread_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ai_messages (
    message_id BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES ai_conversations(conversation_id),
    run_id UUID,
    seq BIGINT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, seq)
);

CREATE TABLE auth_nonces (
    nonce TEXT PRIMARY KEY,
    key_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_auth_nonces_expiry ON auth_nonces (expires_at);
