"""Deterministic enterprise ranking layer — not an ML reranker.

Combines the backend's relevance score with policy-driven preferences into
one explainable score: relevance is the dominant term; authority/freshness/
context bonuses can only nudge ranking, never override relevance (their sum
is well under the relevance score's own weight). Weights are named constants
so tuning them means editing values here, not hunting for magic numbers.

Swap this module (or the RetrievalBackend it scores results from) to change
ranking behaviour without touching KnowledgeService's orchestration.
"""

from typing import Any

from app.knowledge.models.enums import FreshnessPreference, KnowledgeAuthority
from app.knowledge.models.retrieval import RetrievalResult
from app.knowledge.policies import RetrievalPolicy

RELEVANCE_WEIGHT = 1.0

AUTHORITY_BONUS: dict[KnowledgeAuthority, float] = {
    KnowledgeAuthority.OFFICIAL_POLICY: 0.15,
    KnowledgeAuthority.APPROVED_STANDARD: 0.12,
    KnowledgeAuthority.REFERENCE_ARCHITECTURE: 0.10,
    KnowledgeAuthority.TEAM_DOCUMENTATION: 0.05,
    KnowledgeAuthority.HISTORICAL: 0.0,
    KnowledgeAuthority.UNVERIFIED: -0.05,
}

FRESHNESS_BONUS = 0.08
TECHNOLOGY_MATCH_BONUS = 0.05
SERVICE_MATCH_BONUS = 0.05
ENVIRONMENT_MATCH_BONUS = 0.05


def _raw_score(
    result: RetrievalResult,
    policy: RetrievalPolicy,
    *,
    is_freshest: bool,
    technology_match: bool,
    service_match: bool,
    environment_match: bool,
) -> float:
    """Unclamped combined score, used only as the sort key. Bonuses can push
    this above 1.0 (e.g. a perfect-relevance result with an authority bonus) —
    clamping happens separately, only on the value stored back into
    RetrievalResult.relevance_score, so two results tied at the relevance
    ceiling can still be told apart in ranking order without the display
    score exceeding its documented [0, 1] range.
    """
    score = RELEVANCE_WEIGHT * result.relevance_score
    score += AUTHORITY_BONUS.get(result.authority_level, 0.0)
    if is_freshest and policy.freshness_preference != FreshnessPreference.NONE:
        score += FRESHNESS_BONUS
    if technology_match:
        score += TECHNOLOGY_MATCH_BONUS
    if service_match:
        score += SERVICE_MATCH_BONUS
    if environment_match:
        score += ENVIRONMENT_MATCH_BONUS
    return score


def apply_enterprise_ranking(
    results: list[RetrievalResult], policy: RetrievalPolicy, context_filters: dict[str, Any]
) -> list[RetrievalResult]:
    """Returns new, re-ranked RetrievalResult objects (rank starts at 1, sorted
    by adjusted score descending). Provenance fields (chunk_id, document_id,
    source, knowledge_type, authority_level) are copied through unchanged —
    only relevance_score and rank are replaced.
    """
    freshest_effective_from = None
    for result in results:
        effective_from = result.metadata.get("effective_from")
        if effective_from and (freshest_effective_from is None or effective_from > freshest_effective_from):
            freshest_effective_from = effective_from

    technology_match = bool(context_filters.get("technology"))
    service_match = bool(context_filters.get("service"))
    environment_match = bool(context_filters.get("environment"))

    scored: list[tuple[float, RetrievalResult]] = []
    for result in results:
        is_freshest = bool(freshest_effective_from) and result.metadata.get("effective_from") == freshest_effective_from
        raw = _raw_score(
            result,
            policy,
            is_freshest=is_freshest,
            technology_match=technology_match,
            service_match=service_match,
            environment_match=environment_match,
        )
        clamped = max(0.0, min(1.0, raw))
        scored.append((raw, result.model_copy(update={"relevance_score": clamped})))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    ranked: list[RetrievalResult] = []
    for rank, (_, result) in enumerate(scored, start=1):
        result.rank = rank
        ranked.append(result)
    return ranked
