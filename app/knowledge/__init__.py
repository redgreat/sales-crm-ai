"""知识授权索引边界（需求 4.8/4.16；主档权限留 CRM）。"""
from app.knowledge.store import (
    disable_knowledge,
    doc_as_fact,
    search_authorized,
    upsert_document,
)

__all__ = ["disable_knowledge", "doc_as_fact", "search_authorized", "upsert_document"]
