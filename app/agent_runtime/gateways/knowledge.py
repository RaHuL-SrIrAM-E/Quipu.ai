"""KnowledgeGateway — abstraction only. No retrieval, embeddings, vector search,
reranking, or Google services here.

Deliberately kept returning list[KnowledgeItem] (not the richer KnowledgeContext
from app.knowledge) — this is the agent-facing surface, and agents shouldn't need
chunk-level provenance, ranking internals, or retrieval strategy.
KnowledgeServiceGateway below is that narrowing adapter, built in Level 1.5
once an agent (Planning) actually needed a working gateway: it calls a
KnowledgeService and narrows the returned KnowledgeContext down via
app.knowledge.gateway_adapter. See docs/architecture/knowledge_platform.md.
"""

from typing import Protocol, runtime_checkable

from app.domain import KnowledgeItem, KnowledgeRequest
from app.knowledge.gateway_adapter import knowledge_context_to_items
from app.knowledge.service import KnowledgeService


@runtime_checkable
class KnowledgeGateway(Protocol):
    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]: ...


class KnowledgeServiceGateway:
    """Delegates to a KnowledgeService (LocalKnowledgeService, in-memory or
    Google-backed) and narrows its KnowledgeContext to KnowledgeItem[]."""

    def __init__(self, service: KnowledgeService):
        self._service = service

    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]:
        context = await self._service.search(request)
        return knowledge_context_to_items(context)
