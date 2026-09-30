"""知识授权索引边界（需求 4.8/4.16；主档权限留 CRM）。"""
from app.knowledge.store import (
    authorized_knowledge_ids,
    cited_knowledge_ids,
    disable_knowledge,
    doc_as_fact,
    filter_unauthorized_history,
    search_authorized,
    upsert_document,
    validate_scope,
)

__all__ = [
    "authorized_knowledge_ids",
    "cited_knowledge_ids",
    "disable_knowledge",
    "doc_as_fact",
    "filter_unauthorized_history",
    "search_authorized",
    "upsert_document",
    "validate_scope",
]
