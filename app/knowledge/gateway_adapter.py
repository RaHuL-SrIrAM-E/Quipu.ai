"""Bridges the rich, service-facing KnowledgeContext down to the thin,
agent-facing KnowledgeItem[] that app.agent_runtime.gateways.knowledge.KnowledgeGateway
returns. See docs/architecture/knowledge_platform.md for the full reasoning:
the gateway stays deliberately simple; only the service sees chunk-level
provenance, ranking internals, and retrieval strategy.

Pure mapping functions — no I/O, no retrieval logic. A concrete KnowledgeGateway
implementation (future work) would call a KnowledgeService then pass its
KnowledgeContext through here before returning to the agent.
"""

from app.domain import KnowledgeItem
from app.knowledge.models.context import KnowledgeContext
from app.knowledge.models.retrieval import RetrievalResult


def retrieval_result_to_knowledge_item(result: RetrievalResult) -> KnowledgeItem:
    return KnowledgeItem(
        document_id=result.document_id,
        title=result.metadata.get("title", result.document_id),
        content=result.content,
        knowledge_type=result.knowledge_type,
        source=result.source,
        relevance_score=result.relevance_score,
        metadata={**result.metadata, "chunk_id": result.chunk_id, "rank": result.rank},
    )


def knowledge_context_to_items(context: KnowledgeContext) -> list[KnowledgeItem]:
    return [retrieval_result_to_knowledge_item(result) for result in context.results]
