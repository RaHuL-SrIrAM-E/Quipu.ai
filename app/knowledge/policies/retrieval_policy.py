"""RetrievalPolicy — describes how a knowledge request should be handled.
Pure data: it never performs retrieval itself.

AGENT_RETRIEVAL_PROFILES gives each Quipu agent a sensible default policy.
These are defaults, not hard restrictions — an agent can still request another
knowledge type when justified; nothing here enforces the allow-list yet.
"""

from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import KnowledgeType
from app.knowledge.models.enums import FreshnessPreference, KnowledgeAuthority


class RetrievalPolicy(BaseModel):
    allowed_knowledge_types: list[KnowledgeType]
    default_top_k: int = Field(default=5, gt=0)
    min_relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    reranking_enabled: bool = False
    freshness_preference: FreshnessPreference = FreshnessPreference.NONE
    authority_preference: list[KnowledgeAuthority] = Field(default_factory=list)
    max_context_items: int = Field(default=10, gt=0)
    metadata_filters: dict[str, Any] = Field(default_factory=dict)
    enforce_validity: bool = True


DEFAULT_RETRIEVAL_POLICY = RetrievalPolicy(allowed_knowledge_types=list(KnowledgeType))

AGENT_RETRIEVAL_PROFILES: dict[str, RetrievalPolicy] = {
    "planning_agent": RetrievalPolicy(
        allowed_knowledge_types=[
            KnowledgeType.ARCHITECTURE_PATTERN,
            KnowledgeType.COMPLIANCE,
            KnowledgeType.TECHNOLOGY_STANDARD,
            KnowledgeType.HISTORICAL_PROJECT,
        ]
    ),
    "architecture_agent": RetrievalPolicy(
        allowed_knowledge_types=[
            KnowledgeType.ARCHITECTURE_PATTERN,
            KnowledgeType.SECURITY_POLICY,
            KnowledgeType.COMPLIANCE,
            KnowledgeType.TECHNOLOGY_STANDARD,
            KnowledgeType.DEPLOYMENT_STANDARD,
        ]
    ),
    "codegen_agent": RetrievalPolicy(
        allowed_knowledge_types=[
            KnowledgeType.CODING_STANDARD,
            KnowledgeType.SECURITY_POLICY,
            KnowledgeType.TECHNOLOGY_STANDARD,
            KnowledgeType.ARCHITECTURE_PATTERN,
        ]
    ),
    "testing_agent": RetrievalPolicy(
        allowed_knowledge_types=[
            KnowledgeType.TESTING_STANDARD,
            KnowledgeType.CODING_STANDARD,
            KnowledgeType.TROUBLESHOOTING,
            KnowledgeType.INCIDENT,
        ]
    ),
    "deployment_agent": RetrievalPolicy(
        allowed_knowledge_types=[
            KnowledgeType.DEPLOYMENT_STANDARD,
            KnowledgeType.SECURITY_POLICY,
            KnowledgeType.COMPLIANCE,
            KnowledgeType.OPERATIONS,
        ]
    ),
    "monitoring_agent": RetrievalPolicy(
        allowed_knowledge_types=[
            KnowledgeType.OPERATIONS,
            KnowledgeType.DEPLOYMENT_STANDARD,
            KnowledgeType.TROUBLESHOOTING,
        ]
    ),
    "detecting_agent": RetrievalPolicy(
        allowed_knowledge_types=[
            KnowledgeType.OPERATIONS,
            KnowledgeType.TROUBLESHOOTING,
            KnowledgeType.INCIDENT,
        ]
    ),
    "incident_resolution_agent": RetrievalPolicy(
        allowed_knowledge_types=[
            KnowledgeType.INCIDENT,
            KnowledgeType.TROUBLESHOOTING,
            KnowledgeType.OPERATIONS,
            KnowledgeType.ARCHITECTURE_PATTERN,
            KnowledgeType.DEPLOYMENT_STANDARD,
        ]
    ),
}


def get_retrieval_policy(agent_name: str) -> RetrievalPolicy:
    return AGENT_RETRIEVAL_PROFILES.get(agent_name, DEFAULT_RETRIEVAL_POLICY)
