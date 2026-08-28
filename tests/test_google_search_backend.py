from types import SimpleNamespace

import pytest
from google.api_core import exceptions as google_exceptions

from app.domain import KnowledgeRequest, KnowledgeType, RetrievalStrategy
from app.knowledge import InMemoryRetrievalBackend, KnowledgeChunk, KnowledgeDocument, LocalKnowledgeService
from app.knowledge.backends.google_search import (
    GoogleAuthError,
    GoogleInvalidRequestError,
    GoogleMalformedResponseError,
    GooglePermissionError,
    GoogleSearchConfig,
    GoogleSearchConfigError,
    GoogleSearchRetrievalBackend,
    GoogleServiceUnavailableError,
    GoogleTimeoutError,
    _build_filter,
)
from app.knowledge.models.enums import KnowledgeAuthority
from app.knowledge.policies import RetrievalPolicy


def make_config(**overrides) -> GoogleSearchConfig:
    defaults = dict(project_id="quipu-project", data_store_id="quipu-datastore")
    defaults.update(overrides)
    return GoogleSearchConfig(**defaults)


def make_request(**overrides) -> KnowledgeRequest:
    defaults = dict(
        agent_name="architecture_agent",
        workflow_id="wf-1",
        query="messaging patterns",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        top_k=5,
    )
    defaults.update(overrides)
    return KnowledgeRequest(**defaults)


def make_policy(**overrides) -> RetrievalPolicy:
    defaults = dict(allowed_knowledge_types=[KnowledgeType.ARCHITECTURE_PATTERN])
    defaults.update(overrides)
    return RetrievalPolicy(**defaults)


def make_fake_result(chunk_id, content, relevance_score, uri="gs://bucket/doc.pdf", struct_data=None, name=None):
    document_metadata = SimpleNamespace(uri=uri, title="Doc Title", struct_data=struct_data or {})
    chunk = SimpleNamespace(
        id=chunk_id,
        content=content,
        relevance_score=relevance_score,
        document_metadata=document_metadata,
        name=name or f"chunks/{chunk_id}",
    )
    return SimpleNamespace(chunk=chunk, document=None, id=chunk_id, model_scores={}, rank_signals=None)


class _AsyncResultIterable:
    def __init__(self, results):
        self._results = results

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for result in self._results:
            yield result


class FakeSearchClient:
    def __init__(self, results=None, raise_error: Exception | None = None):
        self._results = results or []
        self._raise_error = raise_error
        self.last_request = None
        self.last_timeout = None

    async def search(self, request, timeout=None):
        self.last_request = request
        self.last_timeout = timeout
        if self._raise_error:
            raise self._raise_error
        return _AsyncResultIterable(self._results)


# 1. KnowledgeRequest -> Google request mapping
@pytest.mark.asyncio
async def test_request_mapping_query_and_serving_config():
    client = FakeSearchClient(results=[])
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    await backend.search(make_request(query="dark mode"), make_policy())
    assert client.last_request.query == "dark mode"
    assert client.last_request.serving_config == make_config().serving_config_path
    assert client.last_timeout == make_config().timeout_seconds


# 2. Top-k mapping
@pytest.mark.asyncio
async def test_top_k_mapping_oversamples_for_ranking_headroom():
    client = FakeSearchClient(results=[])
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    await backend.search(make_request(top_k=3), make_policy(max_context_items=3))
    assert client.last_request.page_size == 6  # oversample factor 2


# 3. Metadata filter mapping
def test_metadata_filter_mapping_includes_arbitrary_keys():
    filter_expr = _build_filter(make_request(filters={"team": "payments"}), make_policy())
    assert 'team: ANY("payments")' in filter_expr


# 4. Knowledge type mapping
def test_knowledge_type_included_in_filter():
    filter_expr = _build_filter(make_request(), make_policy())
    assert 'knowledge_type: ANY("architecture_pattern")' in filter_expr


def test_typed_context_fields_mapped_directly():
    filter_expr = _build_filter(make_request(filters={"technology": "react", "environment": "production"}), make_policy())
    assert 'technology: ANY("react")' in filter_expr
    assert 'environment: ANY("production")' in filter_expr


# 5. Result -> RetrievalResult mapping
@pytest.mark.asyncio
async def test_result_mapping():
    result = make_fake_result("chunk-1", "Use async messaging.", 0.83, uri="gs://bucket/architecture.pdf")
    client = FakeSearchClient(results=[result])
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    mapped = await backend.search(make_request(), make_policy())
    assert len(mapped) == 1
    assert mapped[0].content == "Use async messaging."
    assert mapped[0].relevance_score == 0.83


# 6. Provenance preservation
@pytest.mark.asyncio
async def test_provenance_preserved():
    result = make_fake_result("chunk-42", "content", 0.5, uri="gs://bucket/policy.pdf")
    client = FakeSearchClient(results=[result])
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    mapped = await backend.search(make_request(), make_policy())
    assert mapped[0].chunk_id == "chunk-42"
    assert mapped[0].source == "gs://bucket/policy.pdf"
    assert mapped[0].metadata["google_chunk_name"] == "chunks/chunk-42"


# 7. Empty Google response
@pytest.mark.asyncio
async def test_empty_google_response():
    client = FakeSearchClient(results=[])
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    mapped = await backend.search(make_request(), make_policy())
    assert mapped == []


# 8. Google authentication error
@pytest.mark.asyncio
async def test_authentication_error_translated():
    client = FakeSearchClient(raise_error=google_exceptions.Unauthenticated("bad creds"))
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    with pytest.raises(GoogleAuthError):
        await backend.search(make_request(), make_policy())


# 9. Google permission error
@pytest.mark.asyncio
async def test_permission_error_translated():
    client = FakeSearchClient(raise_error=google_exceptions.PermissionDenied("no access"))
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    with pytest.raises(GooglePermissionError):
        await backend.search(make_request(), make_policy())


# 10. Google timeout
@pytest.mark.asyncio
async def test_deadline_exceeded_translated_as_timeout():
    client = FakeSearchClient(raise_error=google_exceptions.DeadlineExceeded("too slow"))
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    with pytest.raises(GoogleTimeoutError):
        await backend.search(make_request(), make_policy())


# 11. Google service error
@pytest.mark.asyncio
async def test_service_unavailable_translated():
    client = FakeSearchClient(raise_error=google_exceptions.ServiceUnavailable("down"))
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    with pytest.raises(GoogleServiceUnavailableError):
        await backend.search(make_request(), make_policy())


def test_invalid_argument_translated():
    from app.knowledge.backends.google_search import _translate_error

    translated = _translate_error(google_exceptions.InvalidArgument("bad filter"))
    assert isinstance(translated, GoogleInvalidRequestError)


# 12. Malformed response handling
@pytest.mark.asyncio
async def test_malformed_response_handling():
    bad_result = make_fake_result("chunk-1", "content", relevance_score="not-a-number")
    client = FakeSearchClient(results=[bad_result])
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    with pytest.raises(GoogleMalformedResponseError):
        await backend.search(make_request(), make_policy())


# 13. Configuration validation
def test_config_from_settings_requires_project_and_data_store(monkeypatch):
    from app import config as config_module

    fake_settings = SimpleNamespace(
        gcp_project_id=None,
        discovery_engine_data_store_id=None,
        discovery_engine_location="global",
        discovery_engine_serving_config_id="default_search",
        discovery_engine_timeout_seconds=10.0,
    )
    monkeypatch.setattr(config_module, "get_settings", lambda: fake_settings)
    with pytest.raises(GoogleSearchConfigError):
        GoogleSearchConfig.from_settings()


def test_config_serving_config_path():
    config = make_config(location="us")
    assert config.serving_config_path == (
        "projects/quipu-project/locations/us/collections/default_collection"
        "/dataStores/quipu-datastore/servingConfigs/default_search"
    )


# 14. Strategy mapping
@pytest.mark.asyncio
async def test_google_backend_used_with_hybrid_strategy():
    # Discovery Engine's base Search API doesn't expose a per-request SEMANTIC
    # vs KEYWORD toggle (verified against the installed SDK) — HYBRID is what
    # a service wrapping this backend should declare itself as.
    result = make_fake_result("chunk-1", "content", 0.9)
    client = FakeSearchClient(results=[result])
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    service = LocalKnowledgeService(retrieval_backend=backend, retrieval_strategy=RetrievalStrategy.HYBRID)
    context = await service.search(make_request())
    assert context.retrieval_strategy == RetrievalStrategy.HYBRID


# 15. Compatibility with LocalKnowledgeService
@pytest.mark.asyncio
async def test_compatibility_with_local_knowledge_service_ranking():
    official = make_fake_result(
        "chunk-official", "async messaging patterns", 0.7, struct_data={"authority_level": "official_policy"}
    )
    client = FakeSearchClient(results=[official])
    backend = GoogleSearchRetrievalBackend(make_config(), client=client)
    service = LocalKnowledgeService(retrieval_backend=backend)
    context = await service.search(make_request())
    assert context.results[0].authority_level == KnowledgeAuthority.OFFICIAL_POLICY
    # relevance_score was adjusted upward by Quipu's enterprise ranking (authority bonus)
    assert context.results[0].relevance_score > 0.7


# 16. Existing InMemoryRetrievalBackend behavior remains unchanged
@pytest.mark.asyncio
async def test_in_memory_backend_still_works_unaffected():
    document = KnowledgeDocument(
        document_id="doc-1",
        title="Async messaging",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        authority_level=KnowledgeAuthority.REFERENCE_ARCHITECTURE,
        source="confluence",
        owner="platform-team",
    )
    chunk = KnowledgeChunk(chunk_id="c1", document_id="doc-1", content="messaging patterns", chunk_index=0)
    backend = InMemoryRetrievalBackend([document], [chunk])
    results = await backend.search(make_request(), make_policy())
    assert len(results) == 1
    assert results[0].document_id == "doc-1"
