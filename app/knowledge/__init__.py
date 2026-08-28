"""Quipu's Enterprise Knowledge platform: documents, chunks, retrieval results,
assembled context, retrieval policy, RetrievalBackend abstraction, and the
local KnowledgeService implementation.

Depends on app.domain only. No Google ADK, no Gemini, no vector search, no
Google Cloud services, no real retrieval engine — see
docs/architecture/knowledge_platform.md.
"""

from app.knowledge.backend import RetrievalBackend
from app.knowledge.backends import InMemoryRetrievalBackend
from app.knowledge.gateway_adapter import knowledge_context_to_items, retrieval_result_to_knowledge_item
from app.knowledge.models import (
    ConfidentialityLevel,
    FreshnessPreference,
    KnowledgeAuthority,
    KnowledgeChunk,
    KnowledgeContext,
    KnowledgeDocument,
    RetrievalResult,
)
from app.knowledge.policies import AGENT_RETRIEVAL_PROFILES, DEFAULT_RETRIEVAL_POLICY, RetrievalPolicy, get_retrieval_policy
from app.knowledge.service import InvalidKnowledgeRequestError, KnowledgeService, LocalKnowledgeService

__all__ = [
    "AGENT_RETRIEVAL_PROFILES",
    "ConfidentialityLevel",
    "DEFAULT_RETRIEVAL_POLICY",
    "FreshnessPreference",
    "InMemoryRetrievalBackend",
    "InvalidKnowledgeRequestError",
    "KnowledgeAuthority",
    "KnowledgeChunk",
    "KnowledgeContext",
    "KnowledgeDocument",
    "KnowledgeService",
    "LocalKnowledgeService",
    "RetrievalBackend",
    "RetrievalPolicy",
    "RetrievalResult",
    "get_retrieval_policy",
    "knowledge_context_to_items",
    "retrieval_result_to_knowledge_item",
]
