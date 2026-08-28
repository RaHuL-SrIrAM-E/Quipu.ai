"""Quipu's local Knowledge Service — the retrieval pipeline downstream of any
concrete RetrievalBackend. Framework-agnostic: no Gemini, Vertex AI, or any
Google Cloud service (Level 1.3B-1 scope). See
docs/architecture/knowledge_platform.md for the full pipeline and how a future
Google Agent Search adapter fits in as another RetrievalBackend.

KnowledgeService (below) stays the Protocol from Level 1.3A, unchanged, so
existing structural-typing usage (`isinstance(x, KnowledgeService)`) keeps
working. The concrete implementation is named LocalKnowledgeService rather
than reusing that name — it still satisfies the Protocol structurally (it has
a matching async search()), so isinstance(LocalKnowledgeService(...),
KnowledgeService) is True without inheriting anything.
"""

from typing import Protocol, runtime_checkable

from app.domain import KnowledgeQuery, KnowledgeRequest, RetrievalStrategy
from app.knowledge.backend import RetrievalBackend
from app.knowledge.models.context import KnowledgeContext
from app.knowledge.policies import get_retrieval_policy
from app.knowledge.ranking import apply_enterprise_ranking


@runtime_checkable
class KnowledgeService(Protocol):
    async def search(self, request: KnowledgeRequest) -> KnowledgeContext: ...


class InvalidKnowledgeRequestError(ValueError):
    pass


class LocalKnowledgeService:
    """Concrete KnowledgeService implementation, backend-agnostic via dependency
    injection: LocalKnowledgeService(retrieval_backend=...).

    Pipeline per search(): validate request -> resolve policy -> (backend
    applies hard filters) -> enterprise ranking -> result limits -> assemble
    KnowledgeContext -> append a KnowledgeQuery audit record.
    """

    def __init__(
        self,
        retrieval_backend: RetrievalBackend,
        retrieval_strategy: RetrievalStrategy = RetrievalStrategy.KEYWORD,
    ):
        self._backend = retrieval_backend
        self._retrieval_strategy = retrieval_strategy
        self.audit_log: list[KnowledgeQuery] = []

    async def search(self, request: KnowledgeRequest) -> KnowledgeContext:
        if not request.query or not request.query.strip():
            raise InvalidKnowledgeRequestError("query must not be empty")

        policy = get_retrieval_policy(request.agent_name)

        candidates = await self._backend.search(request, policy)
        candidates = [c for c in candidates if c.relevance_score >= policy.min_relevance_score]

        combined_filters = {**policy.metadata_filters, **request.filters}
        ranked = apply_enterprise_ranking(candidates, policy, combined_filters)

        limit = min(request.top_k, policy.max_context_items)
        selected = ranked[:limit]

        context = KnowledgeContext(
            query=request.query,
            results=selected,
            total_candidates=len(candidates),
            retrieval_strategy=self._retrieval_strategy,
            metadata={"agent_name": request.agent_name, "workflow_id": request.workflow_id},
        )

        self.audit_log.append(
            KnowledgeQuery(
                text=request.query,
                knowledge_type=request.knowledge_type,
                agent_name=request.agent_name,
                workflow_id=request.workflow_id,
                filters=request.filters,
                retrieval_strategy=self._retrieval_strategy,
                top_k=limit,
                result_count=len(selected),
            )
        )

        return context
