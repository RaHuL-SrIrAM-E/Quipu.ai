"""InMemoryRetrievalBackend — a deterministic fake retrieval engine.

Not production-quality search: matching is plain case-insensitive keyword
overlap, not embeddings or a real index. It exists only to validate the
KnowledgeService contract (query matching, hard filtering, top_k, deterministic
scoring) with no external dependency.

Hard filtering happens here, not in KnowledgeService, because this is the only
component with access to full KnowledgeDocument fields (technology, service,
environment, domain, validity window). KnowledgeService decides *what* to
filter by (merging request.filters with policy defaults); this backend
applies it against the actual document/chunk store it was constructed with.
"""

from datetime import datetime, timezone
from typing import Any

from app.domain import KnowledgeRequest
from app.knowledge.models.document import KnowledgeChunk, KnowledgeDocument
from app.knowledge.models.retrieval import RetrievalResult
from app.knowledge.policies import RetrievalPolicy

_TYPED_FILTER_FIELDS = ("domain", "technology", "service", "environment")


def _document_matches_filters(
    document: KnowledgeDocument, filters: dict[str, Any], *, now: datetime, enforce_validity: bool
) -> bool:
    for key, value in filters.items():
        if key in _TYPED_FILTER_FIELDS:
            if getattr(document, key) != value:
                return False
        elif document.metadata.get(key) != value:
            return False

    if enforce_validity:
        if document.effective_from and now < document.effective_from:
            return False
        if document.effective_until and now >= document.effective_until:
            return False
    return True


def _keyword_score(query: str, content: str) -> float:
    query_tokens = {token for token in query.lower().split() if token}
    if not query_tokens:
        return 0.0
    content_lower = content.lower()
    matched = sum(1 for token in query_tokens if token in content_lower)
    return matched / len(query_tokens)


class InMemoryRetrievalBackend:
    def __init__(self, documents: list[KnowledgeDocument], chunks: list[KnowledgeChunk]):
        self._documents = {doc.document_id: doc for doc in documents}
        self._chunks = chunks

    async def search(self, request: KnowledgeRequest, policy: RetrievalPolicy) -> list[RetrievalResult]:
        now = datetime.now(timezone.utc)
        combined_filters = {**policy.metadata_filters, **request.filters}

        candidates: list[tuple[float, KnowledgeChunk, KnowledgeDocument]] = []
        for chunk in self._chunks:
            document = self._documents.get(chunk.document_id)
            if document is None:
                continue
            if document.knowledge_type != request.knowledge_type:
                continue
            if not _document_matches_filters(
                document, combined_filters, now=now, enforce_validity=policy.enforce_validity
            ):
                continue

            score = _keyword_score(request.query, chunk.content)
            if score <= 0.0:
                continue

            candidates.append((score, chunk, document))

        # Deterministic: sort by score desc, stable on ties (preserves input chunk order).
        candidates.sort(key=lambda c: c[0], reverse=True)

        results: list[RetrievalResult] = []
        for rank, (score, chunk, document) in enumerate(candidates, start=1):
            results.append(
                RetrievalResult(
                    chunk_id=chunk.chunk_id,
                    document_id=document.document_id,
                    content=chunk.content,
                    relevance_score=score,
                    rank=rank,
                    source=document.source,
                    knowledge_type=document.knowledge_type,
                    authority_level=document.authority_level,
                    metadata={
                        "document_version": document.version,
                        "effective_from": document.effective_from.isoformat() if document.effective_from else None,
                    },
                )
            )
        return results
