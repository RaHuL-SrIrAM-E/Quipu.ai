"""KnowledgeDocument / KnowledgeChunk — the canonical enterprise knowledge source
and its retrievable pieces. No ingestion or chunking logic here — these are the
shapes a future ingestion pipeline produces and a future retriever consumes.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.enums import KnowledgeType
from app.knowledge.models.enums import ConfidentialityLevel, KnowledgeAuthority


class KnowledgeDocument(BaseModel):
    document_id: str
    title: str
    description: str = ""
    knowledge_type: KnowledgeType
    authority_level: KnowledgeAuthority
    source: str
    owner: str
    version: int = Field(default=1, ge=1)
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    confidentiality: ConfidentialityLevel = ConfidentialityLevel.INTERNAL

    # Filterable dimensions. Free-text rather than enums: the actual vocabularies
    # (which technologies, which services, which environments) are org-specific
    # and not something this contract layer should fix in advance.
    domain: str | None = None
    technology: str | None = None
    service: str | None = None
    environment: str | None = None

    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("effective_from", "effective_until")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validity_window_ordered(self) -> "KnowledgeDocument":
        if self.effective_from and self.effective_until and self.effective_until <= self.effective_from:
            raise ValueError("effective_until must be after effective_from")
        return self


class KnowledgeChunk(BaseModel):
    """One retrievable piece of a KnowledgeDocument. Must retain document_id — that's
    the provenance link retrieval results trace back through."""

    chunk_id: str
    document_id: str
    content: str
    chunk_index: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def _content_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value
