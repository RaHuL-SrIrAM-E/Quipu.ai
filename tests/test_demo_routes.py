"""Tests for the opt-in demo-scenario-seeding endpoint
(app/api/routes/demo.py). See docs/architecture/end_to_end_demo.md
'Seeding a live API'.
"""

import pytest
from starlette.testclient import TestClient

from app.api.app import create_app
from app.api.container import build_memory_container
from app.config import Settings


class _FakeJiraClient:
    def __init__(self):
        self._counter = 0

    def create_story(self, summary: str, description: str) -> dict:
        self._counter += 1
        return {"key": f"QUIPU-{self._counter}", "url": f"https://example.atlassian.net/browse/QUIPU-{self._counter}"}


@pytest.fixture
def container():
    return build_memory_container(jira_client=_FakeJiraClient())


@pytest.fixture
def disabled_client(container):
    app = create_app(container=container)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def enabled_client(container, monkeypatch):
    monkeypatch.setattr("app.api.app.get_settings", lambda: Settings(demo_endpoints_enabled=True))
    app = create_app(container=container)
    return TestClient(app, raise_server_exceptions=False)


def test_demo_endpoints_disabled_by_default(disabled_client):
    r = disabled_client.post("/demo/scenarios/feature")
    assert r.status_code == 404


def test_demo_feature_scenario_seeds_visible_data(enabled_client, container):
    r = enabled_client.post("/demo/scenarios/feature")
    assert r.status_code == 200
    body = r.json()
    assert body["scenario"] == "feature"
    assert body["workflow_id"] is not None
    assert body["detection_id"] is not None
    assert body["review_id"] is not None
    assert body["verification_status"] == "passed"
    assert body["already_seeded"] is False

    # Genuinely visible through the normal query endpoints, not just the response body.
    # (run_feature_flow() also seeds post-deployment MonitoringAgent
    # signals beyond the original feedback batch — signal_ids only lists
    # the latter, so the repo-wide count is >=, not ==.)
    signals = enabled_client.get("/signals").json()
    detections = enabled_client.get("/detections").json()
    reviews = enabled_client.get("/feature-reviews").json()
    workflows = enabled_client.get("/workflows").json()
    assert len(signals) >= len(body["signal_ids"]) > 0
    assert any(d["detection_id"] == body["detection_id"] for d in detections)
    assert any(rv["review_id"] == body["review_id"] for rv in reviews)
    assert any(w["workflow_id"] == body["workflow_id"] for w in workflows)


def test_demo_incident_scenario_seeds_verifications(enabled_client):
    r = enabled_client.post("/demo/scenarios/incident")
    assert r.status_code == 200
    body = r.json()
    assert body["scenario"] == "incident"
    assert body["resolution_id"] is not None

    verifications = enabled_client.get("/verifications").json()
    assert len(verifications) >= 1


def test_demo_scenario_allow_list_rejects_arbitrary_names(enabled_client):
    r = enabled_client.post("/demo/scenarios/anything-else")
    assert r.status_code == 422


def test_demo_scenario_seeding_is_idempotent(enabled_client):
    first = enabled_client.post("/demo/scenarios/feature").json()
    assert first["already_seeded"] is False
    signals_after_first = len(enabled_client.get("/signals").json())

    second = enabled_client.post("/demo/scenarios/feature").json()
    assert second["already_seeded"] is True
    assert second["workflow_id"] == first["workflow_id"]
    assert second["detection_id"] == first["detection_id"]

    # No duplicate data was created by the second (cached) call.
    signals_after_second = len(enabled_client.get("/signals").json())
    assert signals_after_second == signals_after_first


def test_demo_scenarios_are_independent(enabled_client):
    feature = enabled_client.post("/demo/scenarios/feature").json()
    incident = enabled_client.post("/demo/scenarios/incident").json()
    assert feature["workflow_id"] != incident["workflow_id"]
    assert feature["detection_id"] != incident["detection_id"]
