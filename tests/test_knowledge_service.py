from datetime import datetime, timedelta, timezone

import pytest

from app.domain import KnowledgeItem, KnowledgeRequest, KnowledgeType
from app.knowledge import (
    InMemoryRetrievalBackend,
    InvalidKnowledgeRequestError,
    KnowledgeAuthority,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeService,
    LocalKnowledgeService,
    RetrievalPolicy,
    knowledge_context_to_items,
)
from app.knowledge.gateway_adapter import retrieval_result_to_knowledge_item

NOW = datetime.now(timezone.utc)


def doc(**overrides) -> KnowledgeDocument:
    defaults = dict(
        document_id="doc-arch-1",
        title="Async messaging patterns",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        authority_level=KnowledgeAuthority.REFERENCE_ARCHITECTURE,
        source="confluence",
        owner="platform-team",
        version=1,
    )
    defaults.update(overrides)
    return KnowledgeDocument(**defaults)


def chunk(**overrides) -> KnowledgeChunk:
    defaults = dict(chunk_id="chunk-1", document_id="doc-arch-1", content="Use async messaging patterns.", chunk_index=0)
    defaults.update(overrides)
    return KnowledgeChunk(**defaults)


def request(**overrides) -> KnowledgeRequest:
    defaults = dict(
        agent_name="architecture_agent",
        workflow_id="wf-1",
        query="messaging patterns",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
    )
    defaults.update(overrides)
    return KnowledgeRequest(**defaults)


def make_service(documents, chunks) -> LocalKnowledgeService:
    return LocalKnowledgeService(InMemoryRetrievalBackend(documents, chunks))


# 1. Basic retrieval
@pytest.mark.asyncio
async def test_basic_retrieval():
    service = make_service([doc()], [chunk()])
    context = await service.search(request())
    assert len(context.results) == 1
    assert context.results[0].document_id == "doc-arch-1"


# 2. Knowledge type filtering
@pytest.mark.asyncio
async def test_knowledge_type_filtering():
    other = doc(document_id="doc-code-1", knowledge_type=KnowledgeType.CODING_STANDARD)
    other_chunk = chunk(chunk_id="chunk-2", document_id="doc-code-1", content="messaging patterns in code style")
    service = make_service([doc(), other], [chunk(), other_chunk])
    context = await service.search(request(knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN))
    assert all(r.knowledge_type == KnowledgeType.ARCHITECTURE_PATTERN for r in context.results)
    assert context.results[0].document_id == "doc-arch-1"


# 3. Metadata filtering
@pytest.mark.asyncio
async def test_metadata_filtering():
    matching = doc(document_id="doc-a", metadata={"team": "payments"})
    other = doc(document_id="doc-b", metadata={"team": "search"})
    chunks = [
        chunk(chunk_id="c1", document_id="doc-a", content="messaging patterns"),
        chunk(chunk_id="c2", document_id="doc-b", content="messaging patterns"),
    ]
    service = make_service([matching, other], chunks)
    context = await service.search(request(filters={"team": "payments"}))
    assert [r.document_id for r in context.results] == ["doc-a"]


# 4. Technology filtering
@pytest.mark.asyncio
async def test_technology_filtering():
    matching = doc(document_id="doc-a", technology="react")
    other = doc(document_id="doc-b", technology="angular")
    chunks = [
        chunk(chunk_id="c1", document_id="doc-a", content="messaging patterns"),
        chunk(chunk_id="c2", document_id="doc-b", content="messaging patterns"),
    ]
    service = make_service([matching, other], chunks)
    context = await service.search(request(filters={"technology": "react"}))
    assert [r.document_id for r in context.results] == ["doc-a"]


# 5. Service filtering
@pytest.mark.asyncio
async def test_service_filtering():
    matching = doc(document_id="doc-a", service="checkout-api")
    other = doc(document_id="doc-b", service="search-api")
    chunks = [
        chunk(chunk_id="c1", document_id="doc-a", content="messaging patterns"),
        chunk(chunk_id="c2", document_id="doc-b", content="messaging patterns"),
    ]
    service = make_service([matching, other], chunks)
    context = await service.search(request(filters={"service": "checkout-api"}))
    assert [r.document_id for r in context.results] == ["doc-a"]


# 6. Environment filtering
@pytest.mark.asyncio
async def test_environment_filtering():
    matching = doc(document_id="doc-a", environment="production")
    other = doc(document_id="doc-b", environment="staging")
    chunks = [
        chunk(chunk_id="c1", document_id="doc-a", content="messaging patterns"),
        chunk(chunk_id="c2", document_id="doc-b", content="messaging patterns"),
    ]
    service = make_service([matching, other], chunks)
    context = await service.search(request(filters={"environment": "production"}))
    assert [r.document_id for r in context.results] == ["doc-a"]


# 7. Validity-window filtering
@pytest.mark.asyncio
async def test_validity_window_filtering_excludes_expired_document():
    expired = doc(document_id="doc-expired", effective_from=NOW - timedelta(days=30), effective_until=NOW - timedelta(days=1))
    valid = doc(document_id="doc-valid")
    chunks = [
        chunk(chunk_id="c1", document_id="doc-expired", content="messaging patterns"),
        chunk(chunk_id="c2", document_id="doc-valid", content="messaging patterns"),
    ]
    service = make_service([expired, valid], chunks)
    context = await service.search(request())
    assert [r.document_id for r in context.results] == ["doc-valid"]


@pytest.mark.asyncio
async def test_validity_window_not_yet_effective_excluded():
    future = doc(document_id="doc-future", effective_from=NOW + timedelta(days=1))
    service = make_service([future], [chunk(document_id="doc-future", content="messaging patterns")])
    context = await service.search(request())
    assert context.results == []


# 8. Top-k limiting
@pytest.mark.asyncio
async def test_top_k_limiting():
    documents = [doc(document_id=f"doc-{i}") for i in range(5)]
    chunks = [chunk(chunk_id=f"c{i}", document_id=f"doc-{i}", content="messaging patterns") for i in range(5)]
    service = make_service(documents, chunks)
    context = await service.search(request(top_k=2))
    assert len(context.results) == 2
    assert context.total_candidates == 5


# 9. Authority preference
@pytest.mark.asyncio
async def test_authority_preference_breaks_relevance_tie():
    official = doc(document_id="doc-official", authority_level=KnowledgeAuthority.OFFICIAL_POLICY)
    unverified = doc(document_id="doc-unverified", authority_level=KnowledgeAuthority.UNVERIFIED)
    chunks = [
        chunk(chunk_id="c1", document_id="doc-official", content="messaging patterns"),
        chunk(chunk_id="c2", document_id="doc-unverified", content="messaging patterns"),
    ]
    service = make_service([official, unverified], chunks)
    context = await service.search(request())
    assert context.results[0].document_id == "doc-official"


# 10. Freshness preference
@pytest.mark.asyncio
async def test_freshness_preference_prefers_newer_document():
    older = doc(document_id="doc-older", effective_from=NOW - timedelta(days=100))
    newer = doc(document_id="doc-newer", effective_from=NOW - timedelta(days=1))
    chunks = [
        chunk(chunk_id="c1", document_id="doc-older", content="messaging patterns"),
        chunk(chunk_id="c2", document_id="doc-newer", content="messaging patterns"),
    ]
    from app.knowledge.models.enums import FreshnessPreference
    from app.knowledge.policies import retrieval_policy as policy_module

    fresh_policy = RetrievalPolicy(
        allowed_knowledge_types=[KnowledgeType.ARCHITECTURE_PATTERN],
        freshness_preference=FreshnessPreference.PREFER_RECENT,
    )
    policy_module.AGENT_RETRIEVAL_PROFILES["freshness_test_agent"] = fresh_policy
    try:
        service = make_service([older, newer], chunks)
        context = await service.search(request(agent_name="freshness_test_agent"))
        assert context.results[0].document_id == "doc-newer"
    finally:
        del policy_module.AGENT_RETRIEVAL_PROFILES["freshness_test_agent"]


# 11. Backend relevance + enterprise ranking
@pytest.mark.asyncio
async def test_backend_relevance_influences_final_ranking():
    strong = doc(document_id="doc-strong")
    weak = doc(document_id="doc-weak")
    chunks = [
        chunk(chunk_id="c1", document_id="doc-strong", content="messaging patterns messaging patterns"),
        chunk(chunk_id="c2", document_id="doc-weak", content="messaging only, no second term"),
    ]
    service = make_service([strong, weak], chunks)
    context = await service.search(request(query="messaging patterns"))
    assert context.results[0].document_id == "doc-strong"


# 12. Provenance preservation
@pytest.mark.asyncio
async def test_provenance_preserved_through_ranking():
    service = make_service([doc()], [chunk()])
    context = await service.search(request())
    result = context.results[0]
    assert result.chunk_id == "chunk-1"
    assert result.document_id == "doc-arch-1"
    assert result.source == "confluence"
    assert result.knowledge_type == KnowledgeType.ARCHITECTURE_PATTERN
    assert result.authority_level == KnowledgeAuthority.REFERENCE_ARCHITECTURE


# 13. Unknown-agent default retrieval profile
@pytest.mark.asyncio
async def test_unknown_agent_uses_default_profile():
    service = make_service([doc()], [chunk()])
    context = await service.search(request(agent_name="some_future_agent"))
    assert len(context.results) == 1  # DEFAULT profile allows ARCHITECTURE_PATTERN


# 14. Known-agent retrieval profile
@pytest.mark.asyncio
async def test_known_agent_profile_applies_max_context_items():
    documents = [doc(document_id=f"doc-{i}") for i in range(20)]
    chunks = [chunk(chunk_id=f"c{i}", document_id=f"doc-{i}", content="messaging patterns") for i in range(20)]
    service = make_service(documents, chunks)
    context = await service.search(request(agent_name="architecture_agent", top_k=100))
    from app.knowledge.policies import get_retrieval_policy

    policy = get_retrieval_policy("architecture_agent")
    assert len(context.results) == policy.max_context_items


# 15. Empty result set
@pytest.mark.asyncio
async def test_empty_result_set_when_nothing_matches_query():
    service = make_service([doc()], [chunk()])
    context = await service.search(request(query="totally unrelated topic"))
    assert context.results == []
    assert context.total_candidates == 0


# 16. Multiple candidates with deterministic ordering
@pytest.mark.asyncio
async def test_multiple_candidates_deterministic_ordering():
    documents = [doc(document_id=f"doc-{i}") for i in range(4)]
    chunks = [chunk(chunk_id=f"c{i}", document_id=f"doc-{i}", content="messaging patterns") for i in range(4)]
    service = make_service(documents, chunks)
    order_1 = [r.document_id for r in (await service.search(request())).results]
    order_2 = [r.document_id for r in (await service.search(request())).results]
    assert order_1 == order_2


# 17. KnowledgeContext creation
@pytest.mark.asyncio
async def test_knowledge_context_shape():
    service = make_service([doc()], [chunk()])
    context = await service.search(request())
    assert context.query == "messaging patterns"
    assert context.metadata["agent_name"] == "architecture_agent"
    assert context.generated_at.tzinfo is not None


# 18. Gateway compatibility
@pytest.mark.asyncio
async def test_gateway_compatibility_via_adapter():
    service = make_service([doc()], [chunk()])
    context = await service.search(request())
    items = knowledge_context_to_items(context)
    assert all(isinstance(item, KnowledgeItem) for item in items)
    assert items[0].document_id == "doc-arch-1"
    assert retrieval_result_to_knowledge_item(context.results[0]).content == context.results[0].content
    assert isinstance(service, KnowledgeService)


# 19. Invalid request handling
@pytest.mark.asyncio
async def test_invalid_request_empty_query_rejected():
    service = make_service([doc()], [chunk()])
    with pytest.raises(InvalidKnowledgeRequestError):
        await service.search(request(query="   "))


# 20. Naive datetime rejection where applicable
def test_naive_datetime_rejected_on_document():
    with pytest.raises(Exception):
        doc(effective_from=datetime.now())  # naive, no tzinfo
