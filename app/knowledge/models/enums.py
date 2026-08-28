"""Taxonomies scoped to the Knowledge platform itself — not needed by app.domain
or app.agent_runtime, so they live here rather than in app/domain/enums.py.
(RetrievalStrategy is the one exception: it lives in app.domain.enums because
the domain KnowledgeQuery audit record needs it too. Re-exported below.)
"""

from enum import StrEnum

from app.domain.enums import RetrievalStrategy

__all__ = ["ConfidentialityLevel", "FreshnessPreference", "KnowledgeAuthority", "RetrievalStrategy"]


class KnowledgeAuthority(StrEnum):
    """Source authority, so future ranking can prefer official/current sources."""

    OFFICIAL_POLICY = "official_policy"
    APPROVED_STANDARD = "approved_standard"
    REFERENCE_ARCHITECTURE = "reference_architecture"
    TEAM_DOCUMENTATION = "team_documentation"
    HISTORICAL = "historical"
    UNVERIFIED = "unverified"


class ConfidentialityLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class FreshnessPreference(StrEnum):
    """A RetrievalPolicy's soft preference for recency — describes ranking
    behaviour, not a hard filter (see docs/architecture/knowledge_platform.md)."""

    NONE = "none"
    PREFER_RECENT = "prefer_recent"
    REQUIRE_CURRENT = "require_current"
