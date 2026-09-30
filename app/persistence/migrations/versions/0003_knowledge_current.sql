-- 0003 知识"当前版本"与撤权留痕（移动端 PRD 对齐 M-10）。
--
-- 背景：M-10 要求"只检索已发布当前版本"。0002 只有 (knowledge_id, version) 主键，
-- 检索时用 `ORDER BY version DESC` 选最新——version 是 TEXT，字符串序会让 "v10" < "v9"，
-- 选错版本。故引入显式 is_current 标记：当前版本由 CRM 发布时声明，AI 不猜。
--
-- 另加撤权留痕列：撤权后不仅要从检索集合消失，还要能追溯被撤权的内容
-- （用于判定历史会话中哪些旧回答引用了已撤权知识，防止其再次进入模型）。
ALTER TABLE ai_knowledge_docs
    ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;

-- 同一 knowledge_id 最多一个当前版本（服务端强制，避免 AI 侧歧义）
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_current
    ON ai_knowledge_docs (knowledge_id) WHERE is_current;

CREATE INDEX IF NOT EXISTS idx_knowledge_current
    ON ai_knowledge_docs (knowledge_id, is_current) WHERE status = 'published';
