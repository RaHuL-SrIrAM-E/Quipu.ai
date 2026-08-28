"""KnowledgeGateway — abstraction only. No retrieval, embeddings, vector search,
reranking, or Google services here; a real Knowledge Service client implements this later.

Deliberately kept returning list[KnowledgeItem] (not the richer KnowledgeContext
from app.knowledge) — this is the agent-facing surface, and agents shouldn't need
chunk-level provenance, ranking internals, or retrieval strategy. A concrete
gateway implementation calls a KnowledgeService and narrows its KnowledgeContext
down via app.knowledge.gateway_adapter. See docs/architecture/knowledge_platform.md.
"""

from typing import Protocol, runtime_checkable

from app.domain import KnowledgeItem, KnowledgeRequest


@runtime_checkable
class KnowledgeGateway(Protocol):
    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]: ...
