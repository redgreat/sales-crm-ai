-- 0005 增强输入识别产物（P5-INPUT 任务 2，需求 12.2/12.3）。
-- 识别结果独立于 LLM 整理阶段持久化：同 (file_id, source_version, processing, config_version)
-- 的成功结果直接复用——重试 LLM 不重做识别；供应商 task_id 保留供超时后查原任务。
-- 只持久化文件标识与文本/锚点，不落签名 URL（需求 12.3：URL 短时有效、重新鉴权获取）。

CREATE TABLE ai_file_recognitions (
    recognition_id UUID PRIMARY KEY,
    run_id UUID,
    file_id TEXT NOT NULL,
    source_version INTEGER NOT NULL DEFAULT 1,
    processing TEXT NOT NULL CHECK (processing IN ('asr','ocr','parse')),
    provider TEXT NOT NULL DEFAULT '',
    provider_task_id TEXT NOT NULL DEFAULT '',
    config_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','succeeded','failed','cancelled')),
    raw_text TEXT NOT NULL DEFAULT '',
    anchored_text TEXT NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    duration_ms INTEGER,
    error JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- 复用语义在库层兜底：同一文件+版本+处理类型+配置至多一条成功结果
CREATE UNIQUE INDEX idx_recognitions_reuse ON ai_file_recognitions (file_id, source_version, processing, config_version)
    WHERE status = 'succeeded';
CREATE INDEX idx_recognitions_file ON ai_file_recognitions (file_id, created_at DESC);
CREATE INDEX idx_recognitions_run ON ai_file_recognitions (run_id) WHERE run_id IS NOT NULL;
