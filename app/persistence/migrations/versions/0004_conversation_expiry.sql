-- 0004 会话过期与消息幂等（P3 收口）。
-- 1) 会话状态增加 expired：TTL 到期由系统标记；M-01 红线——会话过期只影响
--    会话恢复（不能再续发消息），绝不改变任何候选业务状态（候选归 CRM）。
-- 2) ai_messages 增加 client_key：同会话内客户端幂等键唯一，
--    弱网重复提交（同 key）不再重复落消息（M-07：重复提交返回原 Run）。
--    助手消息不持 client_key，不受该索引约束。

ALTER TABLE ai_conversations DROP CONSTRAINT ai_conversations_status_check;
ALTER TABLE ai_conversations ADD CONSTRAINT ai_conversations_status_check
    CHECK (status IN ('active','closed','expired'));

ALTER TABLE ai_messages ADD COLUMN client_key TEXT;

CREATE UNIQUE INDEX idx_messages_client_key
    ON ai_messages (conversation_id, client_key)
    WHERE client_key IS NOT NULL;
