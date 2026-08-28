"""Enterprise knowledge contracts. These are request/response shapes only —
the actual Knowledge Service (retrieval + reranking) is a future integration.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import KnowledgeType


class KnowledgeItem(BaseModel):
    document_id: str
    title: str
    content: str
    knowledge_type: KnowledgeType
    source: str
    relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeQuery(BaseModel):
    """A record of one query an agent issued during execution (audit trail, not the request payload)."""

    query_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    text: str
    knowledge_type: KnowledgeType | None = None
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    result_count: int | None = None


class KnowledgeRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str
    workflow_id: str
    query: str
    knowledge_type: KnowledgeType
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=5, gt=0)
    require_reranking: bool = False
