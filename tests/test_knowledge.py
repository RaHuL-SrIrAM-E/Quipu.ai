from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.agent_runtime.gateways.knowledge import KnowledgeGateway
from app.domain import KnowledgeItem, KnowledgeQuery, KnowledgeRequest, KnowledgeType, RetrievalStrategy
from app.knowledge import (
    AGENT_RETRIEVAL_PROFILES,
    ConfidentialityLevel,
    FreshnessPreference,
    KnowledgeAuthority,
    KnowledgeChunk,
    KnowledgeContext,
    KnowledgeDocument,
    KnowledgeService,
    RetrievalPolicy,
    RetrievalResult,
    get_retrieval_policy,
    knowledge_context_to_items,
    retrieval_result_to_knowledge_item,
)


def make_document(**overrides) -> KnowledgeDocument:
    defaults = dict(
        document_id="doc-1",
        title="Microservice communication patterns",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        authority_level=KnowledgeAuthority.REFERENCE_ARCHITECTURE,
        source="confluence",
        owner="platform-team",
    )
    defaults.update(overrides)
    return KnowledgeDocument(**defaults)


def make_result(**overrides) -> RetrievalResult:
    defaults = dict(
        chunk_id="chunk-1",
        document_id="doc-1",
        content="Prefer async messaging over synchronous chaining.",
        relevance_score=0.87,
        rank=1,
        source="confluence",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        authority_level=KnowledgeAuthority.REFERENCE_ARCHITECTURE,
    )
    defaults.update(overrides)
    return RetrievalResult(**defaults)


# 1. KnowledgeDocument creation
def test_knowledge_document_creation():
    doc = make_document(tags=["microservices", "messaging"])
    assert doc.document_id == "doc-1"
    assert doc.version == 1
    assert doc.confidentiality == ConfidentialityLevel.INTERNAL


def test_knowledge_document_requires_timezone_aware_dates():
    with pytest.raises(ValidationError):
        make_document(effective_from=datetime.now())  # naive datetime


def test_knowledge_document_validity_window_ordering():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        make_document(effective_from=now, effective_until=now - timedelta(days=1))


# 2. KnowledgeChunk creation
def test_knowledge_chunk_creation():
    chunk = KnowledgeChunk(chunk_id="chunk-1", document_id="doc-1", content="...", chunk_index=0)
    assert chunk.chunk_index == 0


# 3. Chunk-to-document provenance
def test_chunk_retains_parent_document_id():
    doc = make_document()
    chunk = KnowledgeChunk(chunk_id="chunk-1", document_id=doc.document_id, content="text", chunk_index=0)
    assert chunk.document_id == doc.document_id


# 4. Knowledge authority validation
def test_knowledge_authority_rejects_unknown_member():
    with pytest.raises(ValueError):
        KnowledgeAuthority("not_a_real_authority")


def test_knowledge_document_rejects_invalid_authority():
    with pytest.raises(ValidationError):
        make_document(authority_level="not_a_real_authority")


# 5. Knowledge taxonomy validation
def test_knowledge_type_includes_new_operations_and_incident():
    assert KnowledgeType.OPERATIONS in KnowledgeType
    assert KnowledgeType.INCIDENT in KnowledgeType


def test_knowledge_document_rejects_invalid_knowledge_type():
    with pytest.raises(ValidationError):
        make_document(knowledge_type="not_a_real_type")


# 6. RetrievalPolicy creation
def test_retrieval_policy_creation():
    policy = RetrievalPolicy(
        allowed_knowledge_types=[KnowledgeType.CODING_STANDARD],
        default_top_k=3,
        min_relevance_score=0.5,
        reranking_enabled=True,
        freshness_preference=FreshnessPreference.PREFER_RECENT,
        authority_preference=[KnowledgeAuthority.OFFICIAL_POLICY],
        max_context_items=5,
    )
    assert policy.default_top_k == 3
    assert policy.reranking_enabled is True


def test_retrieval_policy_rejects_invalid_relevance_score():
    with pytest.raises(ValidationError):
        RetrievalPolicy(allowed_knowledge_types=[KnowledgeType.CODING_STANDARD], min_relevance_score=1.5)


# 7. RetrievalResult validation
def test_retrieval_result_creation():
    result = make_result()
    assert result.rank == 1
    assert 0.0 <= result.relevance_score <= 1.0


def test_retrieval_result_rejects_out_of_range_score():
    with pytest.raises(ValidationError):
        make_result(relevance_score=1.2)


def test_retrieval_result_rejects_non_positive_rank():
    with pytest.raises(ValidationError):
        make_result(rank=0)


# 8. KnowledgeContext creation
def test_knowledge_context_creation():
    context = KnowledgeContext(
        query="microservice communication patterns",
        results=[make_result()],
        total_candidates=12,
        retrieval_strategy=RetrievalStrategy.HYBRID,
    )
    assert context.total_candidates == 12
    assert len(context.results) == 1
    assert context.generated_at.tzinfo is not None


# 9. RetrievalStrategy enum validation
def test_retrieval_strategy_rejects_unknown_member():
    with pytest.raises(ValueError):
        RetrievalStrategy("not_a_real_strategy")


def test_knowledge_context_rejects_invalid_strategy():
    with pytest.raises(ValidationError):
        KnowledgeContext(query="x", retrieval_strategy="not_a_real_strategy")


# 10. Agent retrieval profile definitions
def test_agent_retrieval_profiles_cover_expected_agents():
    expected = {
        "planning_agent",
        "architecture_agent",
        "codegen_agent",
        "testing_agent",
        "deployment_agent",
        "monitoring_agent",
        "detecting_agent",
        "incident_resolution_agent",
    }
    assert expected <= set(AGENT_RETRIEVAL_PROFILES.keys())


def test_get_retrieval_policy_known_agent():
    policy = get_retrieval_policy("planning_agent")
    assert KnowledgeType.ARCHITECTURE_PATTERN in policy.allowed_knowledge_types
    assert KnowledgeType.CODING_STANDARD not in policy.allowed_knowledge_types


def test_get_retrieval_policy_unknown_agent_falls_back_to_default():
    policy = get_retrieval_policy("some_future_agent")
    assert set(KnowledgeType) == set(policy.allowed_knowledge_types)


# 11. Fake KnowledgeService search
class FakeKnowledgeService:
    def __init__(self, context: KnowledgeContext):
        self._context = context

    async def search(self, request: KnowledgeRequest) -> KnowledgeContext:
        return self._context


@pytest.mark.asyncio
async def test_fake_knowledge_service_search():
    expected = KnowledgeContext(
        query="dark mode",
        results=[make_result()],
        retrieval_strategy=RetrievalStrategy.SEMANTIC,
    )
    service: KnowledgeService = FakeKnowledgeService(expected)
    request = KnowledgeRequest(
        agent_name="planning_agent",
        workflow_id="wf-1",
        query="dark mode",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
    )
    result = await service.search(request)
    assert result is expected
    assert isinstance(service, KnowledgeService)


# 12. KnowledgeGateway compatibility
class FakeKnowledgeGateway:
    """Structurally satisfies KnowledgeGateway by calling a KnowledgeService and
    narrowing its KnowledgeContext through the gateway adapter — the intended shape
    for a real gateway implementation."""

    def __init__(self, service: KnowledgeService):
        self._service = service

    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]:
        context = await self._service.search(request)
        return knowledge_context_to_items(context)


@pytest.mark.asyncio
async def test_knowledge_gateway_stays_item_list_and_composes_with_service():
    context = KnowledgeContext(
        query="dark mode",
        results=[make_result()],
        retrieval_strategy=RetrievalStrategy.SEMANTIC,
    )
    gateway: KnowledgeGateway = FakeKnowledgeGateway(FakeKnowledgeService(context))
    request = KnowledgeRequest(
        agent_name="planning_agent",
        workflow_id="wf-1",
        query="dark mode",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
    )
    items = await gateway.search(request)
    assert isinstance(gateway, KnowledgeGateway)
    assert all(isinstance(item, KnowledgeItem) for item in items)
    assert items[0].document_id == "doc-1"


# 13. KnowledgeQuery audit representation
def test_knowledge_query_audit_fields():
    query = KnowledgeQuery(
        text="dark mode",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        agent_name="planning_agent",
        workflow_id="wf-1",
        filters={"technology": "react"},
        retrieval_strategy=RetrievalStrategy.HYBRID,
        top_k=5,
        result_count=3,
    )
    assert query.agent_name == "planning_agent"
    assert query.retrieval_strategy == RetrievalStrategy.HYBRID


def test_knowledge_query_still_constructible_with_only_original_fields():
    # Level 1.1 callers that only set the original fields must be unaffected.
    query = KnowledgeQuery(text="dark mode")
    assert query.agent_name is None
    assert query.filters == {}


# 14. Serialization/deserialization round trip
@pytest.mark.parametrize(
    "instance",
    [
        make_document(),
        KnowledgeChunk(chunk_id="chunk-1", document_id="doc-1", content="text", chunk_index=0),
        make_result(),
        KnowledgeContext(query="x", results=[make_result()], retrieval_strategy=RetrievalStrategy.KEYWORD),
        RetrievalPolicy(allowed_knowledge_types=[KnowledgeType.CODING_STANDARD]),
        KnowledgeQuery(text="x", agent_name="planning_agent", retrieval_strategy=RetrievalStrategy.SEMANTIC),
    ],
)
def test_round_trip_serialization(instance):
    model_cls = type(instance)
    restored = model_cls.model_validate_json(instance.model_dump_json())
    assert restored == instance


# 15. Invalid metadata/enum values
def test_invalid_confidentiality_rejected():
    with pytest.raises(ValidationError):
        make_document(confidentiality="not_a_real_level")


def test_invalid_freshness_preference_rejected():
    with pytest.raises(ValidationError):
        RetrievalPolicy(
            allowed_knowledge_types=[KnowledgeType.CODING_STANDARD],
            freshness_preference="not_a_real_preference",
        )


def test_knowledge_chunk_rejects_empty_content():
    with pytest.raises(ValidationError):
        KnowledgeChunk(chunk_id="c1", document_id="doc-1", content="   ", chunk_index=0)
