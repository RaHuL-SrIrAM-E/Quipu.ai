"""KnowledgeContext — the final, assembled knowledge handed to an agent after
retrieval, filtering and ranking. No compression/summarization here yet."""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import RetrievalStrategy
from app.knowledge.models.retrieval import RetrievalResult


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeContext(BaseModel):
    query: str
    results: list[RetrievalResult] = Field(default_factory=list)
    total_candidates: int = Field(default=0, ge=0)
    retrieval_strategy: RetrievalStrategy
    generated_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
