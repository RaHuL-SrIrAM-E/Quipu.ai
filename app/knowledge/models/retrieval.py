"""RetrievalResult — one retrieved chunk, ranked, with provenance preserved
all the way back to its source document. No retrieval logic here."""

from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import KnowledgeType
from app.knowledge.models.enums import KnowledgeAuthority


class RetrievalResult(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    rank: int = Field(ge=1)
    source: str
    knowledge_type: KnowledgeType
    authority_level: KnowledgeAuthority
    metadata: dict[str, Any] = Field(default_factory=dict)
