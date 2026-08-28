"""Real Google Agent Search integration test — NOT part of the normal test run.

Skipped unless QUIPU_RUN_GOOGLE_INTEGRATION_TESTS=true is set, and even then
requires real GCP credentials (Application Default Credentials) plus a
configured DISCOVERY_ENGINE_DATA_STORE_ID pointing at a real datastore with
at least one indexed document. `pytest tests/` never triggers this file's
network call.
"""

import os

import pytest

from app.knowledge.backends.google_search import GoogleSearchConfig, GoogleSearchRetrievalBackend
from app.knowledge.policies import DEFAULT_RETRIEVAL_POLICY

pytestmark = pytest.mark.skipif(
    os.environ.get("QUIPU_RUN_GOOGLE_INTEGRATION_TESTS") != "true",
    reason="set QUIPU_RUN_GOOGLE_INTEGRATION_TESTS=true to run against a real Agent Search datastore",
)


@pytest.mark.asyncio
async def test_real_google_search_returns_results():
    from app.domain import KnowledgeRequest, KnowledgeType

    config = GoogleSearchConfig.from_settings()
    backend = GoogleSearchRetrievalBackend(config)
    request = KnowledgeRequest(
        agent_name="integration_test",
        workflow_id="integration-test",
        query="test",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        top_k=3,
    )
    results = await backend.search(request, DEFAULT_RETRIEVAL_POLICY)
    assert isinstance(results, list)
