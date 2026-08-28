"""KnowledgeGateway — abstraction only. No retrieval, embeddings, vector search,
reranking, or Google services here; a real Knowledge Service client implements this later.
"""

from typing import Protocol, runtime_checkable

from app.domain import KnowledgeItem, KnowledgeRequest


@runtime_checkable
class KnowledgeGateway(Protocol):
    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]: ...
