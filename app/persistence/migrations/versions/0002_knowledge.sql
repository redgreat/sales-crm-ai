-- 0002 知识授权索引：AI 库只存授权索引映射与必要运行数据（需求 4.8/4.16、计划 P5）。
-- 知识主档与权限判定留在 CRM；CRM 通过同步接口写入本索引，
-- dept/角色级授权由 CRM 展开为 users 列表或 public 标记（AI 不理解组织结构）。
CREATE TABLE ai_knowledge_docs (
    knowledge_id TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('published', 'disabled')),
    -- 授权范围：{"public": true} 或 {"users": ["u1", "u2"]}
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (knowledge_id, version)
);

CREATE INDEX idx_knowledge_status ON ai_knowledge_docs (status);
CREATE INDEX idx_knowledge_scope ON ai_knowledge_docs USING GIN (scope);
