"""GoogleSearchRetrievalBackend — a RetrievalBackend implementation backed by
Google's Agent Search (the current name for what was Vertex AI Search /
Vertex AI Search and Conversation / Generative AI App Builder; the Discovery
Engine API is unchanged under the rename). Verified 2026-08 against the
installed `google-cloud-discoveryengine` SDK's actual proto field names —
see docs/architecture/knowledge_platform.md for the verification notes and
known limitations.

This is the ONLY file in Quipu allowed to import the Google Discovery Engine
SDK. KnowledgeGateway, KnowledgeService/LocalKnowledgeService, and every
agent stay ignorant of it — they only ever see RetrievalBackend. app.knowledge
does not import this module in its __init__.py, so `import app.knowledge`
never pulls in the Google SDK; callers who want this backend import it
explicitly: `from app.knowledge.backends.google_search import
GoogleSearchRetrievalBackend`.
"""

from datetime import datetime, timezone
from typing import Any

from google.api_core import exceptions as google_exceptions
from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1 as discoveryengine
from pydantic import BaseModel, Field

from app.core.observability import get_logger
from app.domain import KnowledgeRequest
from app.knowledge.models.enums import KnowledgeAuthority
from app.knowledge.models.retrieval import RetrievalResult
from app.knowledge.policies import RetrievalPolicy

logger = get_logger("quipu.knowledge.google_search")

# Fields with a direct, typed home on KnowledgeDocument. Any other filter key
# is passed through as-is — it's assumed to be an indexed custom struct_data
# field in the target datastore's schema. See "Known limitations" below.
_TYPED_FILTER_FIELDS = ("domain", "technology", "service", "environment")

# Oversample so Quipu's enterprise ranking layer has real candidates to
# reorder, rather than just rubber-stamping Google's own result order.
_OVERSAMPLE_FACTOR = 2
_MAX_PAGE_SIZE = 50


class GoogleSearchConfigError(Exception):
    """Missing/invalid configuration — not a network or API error."""


class GoogleRetrievalError(Exception):
    """Base for all translated Google Search errors. Callers should never need
    to catch google.api_core.exceptions directly — see _translate_error()."""


class GoogleAuthError(GoogleRetrievalError):
    pass


class GooglePermissionError(GoogleRetrievalError):
    pass


class GoogleInvalidRequestError(GoogleRetrievalError):
    pass


class GoogleServiceUnavailableError(GoogleRetrievalError):
    pass


class GoogleTimeoutError(GoogleRetrievalError):
    pass


class GoogleMalformedResponseError(GoogleRetrievalError):
    pass


def _translate_error(exc: Exception) -> GoogleRetrievalError:
    if isinstance(exc, google_exceptions.Unauthenticated):
        return GoogleAuthError(str(exc))
    if isinstance(exc, (google_exceptions.PermissionDenied, google_exceptions.Forbidden)):
        return GooglePermissionError(str(exc))
    if isinstance(exc, google_exceptions.InvalidArgument):
        return GoogleInvalidRequestError(str(exc))
    if isinstance(exc, google_exceptions.DeadlineExceeded):
        return GoogleTimeoutError(str(exc))
    if isinstance(exc, (google_exceptions.ServiceUnavailable, google_exceptions.BadGateway, google_exceptions.GatewayTimeout)):
        return GoogleServiceUnavailableError(str(exc))
    return GoogleRetrievalError(f"unexpected Google Search error: {exc}")


class GoogleSearchConfig(BaseModel):
    """Typed, externalized configuration. No project ID, location, datastore
    ID, or credentials are hard-coded anywhere in this module — all of it
    comes from here, and this in turn is normally built from app.config.Settings.
    """

    project_id: str
    location: str = "global"  # Discovery Engine location — distinct from Vertex model regions
    data_store_id: str
    serving_config_id: str = "default_search"
    timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def serving_config_path(self) -> str:
        return (
            f"projects/{self.project_id}/locations/{self.location}"
            f"/collections/default_collection/dataStores/{self.data_store_id}"
            f"/servingConfigs/{self.serving_config_id}"
        )

    @classmethod
    def from_settings(cls) -> "GoogleSearchConfig":
        """Builds config from app.config.Settings (env-driven). Raises
        GoogleSearchConfigError if required settings are missing, rather than
        constructing a client that would fail confusingly later."""
        from app.config import get_settings

        settings = get_settings()
        if not settings.gcp_project_id:
            raise GoogleSearchConfigError("GCP_PROJECT_ID is not set")
        if not settings.discovery_engine_data_store_id:
            raise GoogleSearchConfigError("DISCOVERY_ENGINE_DATA_STORE_ID is not set")

        return cls(
            project_id=settings.gcp_project_id,
            location=settings.discovery_engine_location,
            data_store_id=settings.discovery_engine_data_store_id,
            serving_config_id=settings.discovery_engine_serving_config_id,
            timeout_seconds=settings.discovery_engine_timeout_seconds,
        )


def _build_filter(request: KnowledgeRequest, policy: RetrievalPolicy) -> str:
    """Builds a Discovery Engine filter expression (`field: ANY("value")`
    clauses ANDed together). Assumes the target datastore's schema indexes
    knowledge_type and the typed context fields as filterable — a deployment
    responsibility, not something this adapter can verify. Quipu's own
    validity-window and min_relevance_score filtering intentionally are NOT
    attempted here — see the module docstring / architecture doc for why.
    """
    combined_filters: dict[str, Any] = {"knowledge_type": request.knowledge_type.value, **policy.metadata_filters, **request.filters}
    clauses = [f'{key}: ANY("{value}")' for key, value in combined_filters.items()]
    return " AND ".join(clauses)


def _extract_content(chunk: Any) -> str:
    content = getattr(chunk, "content", "") or ""
    if content:
        return content
    logger.warning("Google Search chunk %s had no content; falling back to title", getattr(chunk, "id", "?"))
    document_metadata = getattr(chunk, "document_metadata", None)
    return getattr(document_metadata, "title", "") or ""


def _map_chunk_result(result: Any, request: KnowledgeRequest) -> RetrievalResult | None:
    """Maps one SearchResponse.SearchResult (CHUNKS mode) to a RetrievalResult.

    Provenance: chunk.id and chunk.document_metadata.uri (the enterprise
    source URI) are preserved directly. authority_level has no Google
    equivalent — defaulted to UNVERIFIED (documented limitation, not an
    invented value) unless the datastore schema happens to expose it under
    struct_data['authority_level'] matching our enum.
    """
    chunk = getattr(result, "chunk", None)
    if chunk is None or not getattr(chunk, "id", None):
        return None

    document_metadata = getattr(chunk, "document_metadata", None)
    struct_data = dict(getattr(document_metadata, "struct_data", {}) or {})

    content = _extract_content(chunk)
    if not content:
        return None

    relevance_score = getattr(chunk, "relevance_score", None)
    if relevance_score is None:
        relevance_score = 0.5  # Discovery Engine didn't return one; neutral, documented fallback.
    relevance_score = max(0.0, min(1.0, float(relevance_score)))

    authority_raw = struct_data.get("authority_level")
    try:
        authority_level = KnowledgeAuthority(authority_raw) if authority_raw else KnowledgeAuthority.UNVERIFIED
    except ValueError:
        authority_level = KnowledgeAuthority.UNVERIFIED

    source = getattr(document_metadata, "uri", None) or getattr(chunk, "name", "") or "google-agent-search"

    return RetrievalResult(
        chunk_id=chunk.id,
        document_id=struct_data.get("document_id") or getattr(chunk, "name", chunk.id),
        content=content,
        relevance_score=relevance_score,
        rank=1,  # provisional; caller re-numbers after sorting the full page
        source=source,
        knowledge_type=request.knowledge_type,
        authority_level=authority_level,
        metadata={**struct_data, "google_chunk_name": getattr(chunk, "name", None)},
    )


class GoogleSearchRetrievalBackend:
    """RetrievalBackend backed by Google Agent Search. Structurally satisfies
    app.knowledge.backend.RetrievalBackend — inject it into LocalKnowledgeService:

        LocalKnowledgeService(retrieval_backend=GoogleSearchRetrievalBackend(config))

    `client` is injectable for tests — pass a fake with a `search(request=...)`
    method; production code leaves it unset and a real
    discoveryengine.SearchServiceAsyncClient is created lazily on first use
    (never at construction time, so building this object never touches
    credentials or the network).
    """

    def __init__(self, config: GoogleSearchConfig, client: Any = None):
        self._config = config
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            if self._config.location == "global":
                self._client = discoveryengine.SearchServiceAsyncClient()
            else:
                client_options = ClientOptions(
                    api_endpoint=f"{self._config.location}-discoveryengine.googleapis.com"
                )
                self._client = discoveryengine.SearchServiceAsyncClient(client_options=client_options)
        return self._client

    async def search(self, request: KnowledgeRequest, policy: RetrievalPolicy) -> list[RetrievalResult]:
        page_size = min(_MAX_PAGE_SIZE, max(request.top_k, policy.max_context_items) * _OVERSAMPLE_FACTOR)

        search_request = discoveryengine.SearchRequest(
            serving_config=self._config.serving_config_path,
            query=request.query,
            page_size=page_size,
            filter=_build_filter(request, policy),
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                search_result_mode=discoveryengine.SearchRequest.ContentSearchSpec.SearchResultMode.CHUNKS,
            ),
        )

        client = self._get_client()
        try:
            response = await client.search(request=search_request, timeout=self._config.timeout_seconds)
        except google_exceptions.GoogleAPICallError as exc:
            raise _translate_error(exc) from exc
        except TimeoutError as exc:
            raise GoogleTimeoutError(str(exc)) from exc

        try:
            results: list[RetrievalResult] = []
            async for raw_result in response:
                mapped = _map_chunk_result(raw_result, request)
                if mapped is not None:
                    results.append(mapped)
        except google_exceptions.GoogleAPICallError as exc:
            raise _translate_error(exc) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise GoogleMalformedResponseError(f"unexpected Google Search response shape: {exc}") from exc

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        for rank, result in enumerate(results, start=1):
            result.rank = rank
        return results
