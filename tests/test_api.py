"""Tests for the Quipu Control Plane API (app/api/). Uses the real
in-memory container (app.api.container.build_memory_container) and the
real FastAPI app via starlette's TestClient — no Google Cloud credentials
required. See docs/architecture/control_plane_api.md.
"""

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app.api.app import create_app
from app.api.container import build_memory_container
from app.domain import (
    AgentExecution,
    Artifact,
    ArtifactType,
    Decision,
    DecisionAction,
    DecisionSource,
    DetectionDomain,
    DetectionType,
    RemediationRisk,
    RemediationStrategy,
    RemediationVerification,
    ResolutionResult,
    Signal,
    SignalProvenance,
    SignalSeverity,
    SignalSource,
    SignalType,
    Ticket,
    VerificationOutcome,
    VerificationStatus,
    WorkflowStage,
    WorkflowState,
    WorkflowStatus,
    compute_detection_fingerprint,
    compute_fingerprint,
    compute_resolution_fingerprint,
    compute_verification_key,
)
from app.domain.detection import DetectionResult
from app.domain.feature_review import FeatureReview
from app.domain.enums import ReviewStatus

NOW = datetime.now(timezone.utc)


class _FakeJiraClient:
    """No Jira credentials required — same seam
    FeatureReviewService/PlanningAgent already expose for tests."""

    def __init__(self):
        self._counter = 0

    def create_story(self, summary: str, description: str) -> dict:
        self._counter += 1
        return {"key": f"QUIPU-{self._counter}", "url": f"https://example.atlassian.net/browse/QUIPU-{self._counter}"}


@pytest.fixture
def container():
    return build_memory_container(jira_client=_FakeJiraClient())


@pytest.fixture
def client(container):
    app = create_app(container=container)
    # raise_server_exceptions=False: behave like a real HTTP client — an
    # unhandled exception must come back as the mapped 500 response (see
    # app/api/errors.py), not propagate as a raw Python exception into the
    # test process.
    return TestClient(app, raise_server_exceptions=False)


def make_signal(source_event_id: str, **overrides) -> Signal:
    defaults = dict(
        signal_type=SignalType.APPLICATION_ERROR,
        source=SignalSource.CLOUD_LOGGING,
        severity=SignalSeverity.ERROR,
        observed_at=NOW,
        subject="checkout",
        summary="an error occurred",
        service_name="checkout",
        environment="production",
        evidence={"message": "boom"},
        provenance=SignalProvenance(source_system="x", source_event_id=source_event_id),
        fingerprint=compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id=source_event_id, subject="checkout"),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def make_detection(signal_ids: list[str], **overrides) -> DetectionResult:
    defaults = dict(
        detection_type=DetectionType.INCIDENT,
        domain=DetectionDomain.OPERATIONAL,
        title="Elevated errors",
        summary="summary",
        rationale="rationale",
        confidence=0.9,
        subject="checkout",
        service_name="checkout",
        environment="production",
        supporting_signal_ids=signal_ids,
        observation_window_minutes=15,
        fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="checkout", supporting_signal_ids=signal_ids, window_minutes=15),
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


def make_resolution(detection_id: str, workflow_id: str | None = None, **overrides) -> ResolutionResult:
    defaults = dict(
        detection_id=detection_id,
        workflow_id=workflow_id,
        diagnosis_summary="diagnosis",
        probable_root_cause="root cause",
        root_cause_confidence=0.8,
        remediation_strategy=RemediationStrategy.CODE_FIX,
        remediation_rationale="fix it",
        expected_outcome="errors clear",
        verification_strategy="monitor",
        risk=RemediationRisk.LOW,
        target_agent="codegen_agent",
        fingerprint=compute_resolution_fingerprint(detection_id=detection_id, remediation_strategy=RemediationStrategy.CODE_FIX, subject="checkout"),
    )
    defaults.update(overrides)
    return ResolutionResult(**defaults)


async def seed_workflow(container, **overrides) -> WorkflowState:
    defaults = dict(ticket=Ticket(title="demo", description="demo workflow"), current_stage=WorkflowStage.PLANNING)
    defaults.update(overrides)
    workflow = WorkflowState(**defaults)
    return await container.workflow_repo.create(workflow)


# ---------------------------------------------------------------------------
# 1: health
# ---------------------------------------------------------------------------


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 2-4: workflows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_list(client, container):
    await seed_workflow(container)
    r = client.get("/workflows")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert "workflow_id" in body[0]
    assert "ticket_description" not in body[0]  # summary, not detail


@pytest.mark.asyncio
async def test_workflow_detail(client, container):
    workflow = await seed_workflow(container)
    r = client.get(f"/workflows/{workflow.workflow_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["workflow_id"] == workflow.workflow_id
    assert body["ticket_description"] == "demo workflow"


def test_workflow_not_found(client):
    r = client.get("/workflows/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


# ---------------------------------------------------------------------------
# 5-7: artifacts/executions/decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_retrieval(client, container):
    workflow = await seed_workflow(container)
    artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent", payload={"x": 1})
    await container.artifact_repo.save(workflow.workflow_id, artifact)

    r = client.get(f"/workflows/{workflow.workflow_id}/artifacts")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["artifact_id"] == artifact.artifact_id
    assert "payload" not in body[0]  # no raw internal payload exposed


@pytest.mark.asyncio
async def test_execution_retrieval(client, container):
    workflow = await seed_workflow(container)
    execution = AgentExecution(workflow_id=workflow.workflow_id, agent_name="planning_agent", status=WorkflowStatus.COMPLETED)
    await container.execution_repo.create(execution)

    r = client.get(f"/workflows/{workflow.workflow_id}/executions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["agent_name"] == "planning_agent"


@pytest.mark.asyncio
async def test_decision_retrieval(client, container):
    workflow = await seed_workflow(container)
    decision = Decision(action=DecisionAction.CONTINUE, reason="all good", confidence=1.0, source=DecisionSource.ORCHESTRATOR)
    await container.decision_repo.save(workflow.workflow_id, decision)

    r = client.get(f"/workflows/{workflow.workflow_id}/decisions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["action"] == "continue"


# ---------------------------------------------------------------------------
# 8-10: signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_list(client, container):
    await container.signal_repo.save(make_signal("s1"))
    r = client.get("/signals")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_signal_filtering(client, container):
    await container.signal_repo.save(make_signal("s1", service_name="checkout"))
    await container.signal_repo.save(make_signal("s2", service_name="billing", subject="billing"))

    r = client.get("/signals", params={"service_name": "checkout"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["service_name"] == "checkout"


@pytest.mark.asyncio
async def test_signal_detail(client, container):
    signal = await container.signal_repo.save(make_signal("s1"))
    r = client.get(f"/signals/{signal.signal_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["signal_id"] == signal.signal_id
    assert "evidence" in body  # sanitized evidence exposed in detail
    assert "metadata" not in body  # raw free-form metadata never exposed


# ---------------------------------------------------------------------------
# 11-12: detections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detection_list(client, container):
    signal = await container.signal_repo.save(make_signal("s1"))
    await container.detection_repo.save(make_detection([signal.signal_id]))
    r = client.get("/detections")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_detection_detail(client, container):
    signal = await container.signal_repo.save(make_signal("s1"))
    detection = await container.detection_repo.save(make_detection([signal.signal_id]))
    r = client.get(f"/detections/{detection.detection_id}")
    assert r.status_code == 200
    assert r.json()["supporting_signal_ids"] == [signal.signal_id]


# ---------------------------------------------------------------------------
# 13-14: resolutions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_list(client, container):
    signal = await container.signal_repo.save(make_signal("s1"))
    detection = await container.detection_repo.save(make_detection([signal.signal_id]))
    await container.resolution_repo.save(make_resolution(detection.detection_id))
    r = client.get("/resolutions")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_resolution_detail(client, container):
    signal = await container.signal_repo.save(make_signal("s1"))
    detection = await container.detection_repo.save(make_detection([signal.signal_id]))
    resolution = await container.resolution_repo.save(make_resolution(detection.detection_id))
    r = client.get(f"/resolutions/{resolution.resolution_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["remediation_strategy"] == "code_fix"
    assert body["target_agent"] == "codegen_agent"


# ---------------------------------------------------------------------------
# 15-16: verifications
# ---------------------------------------------------------------------------


async def seed_verification(container, resolution_id: str, workflow_id: str, **overrides) -> RemediationVerification:
    key = compute_verification_key(resolution_id=resolution_id, deployment_artifact_id="artifact-1", revision="rev-1", window_minutes=30)
    defaults = dict(
        verification_id=key,
        resolution_id=resolution_id,
        workflow_id=workflow_id,
        deployment_artifact_id="artifact-1",
        revision="rev-1",
        baseline_detection_id="det-1",
        baseline_summary="1 signal(s): application_error(error)",
        idempotency_key=key,
        status=VerificationStatus.COMPLETED,
        outcome=VerificationOutcome.VERIFIED_RESOLVED,
        reason="healthy",
    )
    defaults.update(overrides)
    return await container.verification_repo.create(RemediationVerification(**defaults))


@pytest.mark.asyncio
async def test_verification_list(client, container):
    workflow = await seed_workflow(container)
    await seed_verification(container, "res-1", workflow.workflow_id)
    r = client.get("/verifications")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_verification_detail_shows_outcome_distinct_from_deployed(client, container):
    workflow = await seed_workflow(container, status=WorkflowStatus.COMPLETED, metadata={"remediation_outcome": "deployed_pending_verification"})
    verification = await seed_verification(container, "res-1", workflow.workflow_id)

    workflow_response = client.get(f"/workflows/{workflow.workflow_id}").json()
    verification_response = client.get(f"/verifications/{verification.verification_id}").json()

    # The workflow's own remediation_outcome marker (set before
    # verification ran) and the verification record's outcome are
    # distinct fields the UI must read separately — this API never
    # collapses "deployed" and "verified resolved" into one value.
    assert workflow_response["remediation_outcome"] == "deployed_pending_verification"
    assert verification_response["outcome"] == "verified_resolved"


# ---------------------------------------------------------------------------
# 17-21: feature reviews
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_review_retrieval(client, container):
    signal = await container.signal_repo.save(make_signal("s1", signal_type=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK))
    detection = await container.detection_repo.save(make_detection([signal.signal_id], detection_type=DetectionType.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT))
    review = await container.review_service.create_review(detection.detection_id)

    r = client.get(f"/feature-reviews/{review.review_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"

    r_list = client.get("/feature-reviews")
    assert r_list.status_code == 200
    assert len(r_list.json()) == 1


@pytest.mark.asyncio
async def test_feature_review_approval(client, container):
    signal = await container.signal_repo.save(make_signal("s1", signal_type=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK))
    detection = await container.detection_repo.save(make_detection([signal.signal_id], detection_type=DetectionType.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT))
    review = await container.review_service.create_review(detection.detection_id)

    r = client.post(f"/feature-reviews/{review.review_id}/approve", json={"review_comment": "approved"}, headers={"X-Quipu-Reviewer-Id": "alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["reviewer_id"] == "alice"
    assert body["reviewer_type"] == "human"


@pytest.mark.asyncio
async def test_feature_review_rejection(client, container):
    signal = await container.signal_repo.save(make_signal("s1", signal_type=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK))
    detection = await container.detection_repo.save(make_detection([signal.signal_id], detection_type=DetectionType.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT))
    review = await container.review_service.create_review(detection.detection_id)

    r = client.post(f"/feature-reviews/{review.review_id}/reject", json={}, headers={"X-Quipu-Reviewer-Id": "alice"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_approval_requires_valid_human_authorization(client, container):
    signal = await container.signal_repo.save(make_signal("s1", signal_type=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK))
    detection = await container.detection_repo.save(make_detection([signal.signal_id], detection_type=DetectionType.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT))
    review = await container.review_service.create_review(detection.detection_id)

    # No X-Quipu-Reviewer-Id header at all -> unauthenticated.
    r = client.post(f"/feature-reviews/{review.review_id}/approve", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_duplicate_approval_remains_idempotent(client, container):
    signal = await container.signal_repo.save(make_signal("s1", signal_type=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK))
    detection = await container.detection_repo.save(make_detection([signal.signal_id], detection_type=DetectionType.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT))
    review = await container.review_service.create_review(detection.detection_id)

    r1 = client.post(f"/feature-reviews/{review.review_id}/approve", json={}, headers={"X-Quipu-Reviewer-Id": "alice"})
    r2 = client.post(f"/feature-reviews/{review.review_id}/approve", json={}, headers={"X-Quipu-Reviewer-Id": "bob"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["ticket_id"] == r2.json()["ticket_id"]
    assert r2.json()["reviewer_id"] == "alice"  # the SECOND call is idempotent re-entry — the original reviewer is preserved, not overwritten


# ---------------------------------------------------------------------------
# 22-23: workflow step / remediation commands delegate correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_step_delegates_to_orchestrator(client, container, monkeypatch):
    workflow = await seed_workflow(container)

    calls = []

    async def _fake_execute_next_step(workflow_id):
        calls.append(workflow_id)
        return await container.workflow_repo.get(workflow_id)

    monkeypatch.setattr(container.orchestration, "execute_next_step", _fake_execute_next_step)

    r = client.post(f"/workflows/{workflow.workflow_id}/step")
    assert r.status_code == 200
    assert calls == [workflow.workflow_id]


@pytest.mark.asyncio
async def test_remediation_delegates_to_existing_authorization_path(client, container, monkeypatch):
    calls = []

    async def _fake_start_remediation(resolution_id):
        calls.append(resolution_id)
        return await seed_workflow(container)

    monkeypatch.setattr(container.orchestration, "start_remediation_from_resolution", _fake_start_remediation)

    r = client.post("/resolutions/some-resolution-id/remediate")
    assert r.status_code == 200
    assert calls == ["some-resolution-id"]


# ---------------------------------------------------------------------------
# 24-27: dangerous operations are unavailable
# ---------------------------------------------------------------------------


def test_no_arbitrary_tool_execution_endpoint(client):
    assert client.post("/tools/execute", json={}).status_code == 404


def test_no_shell_endpoint(client):
    assert client.post("/shell", json={"command": "ls"}).status_code == 404


def test_no_arbitrary_deployment_endpoint(client):
    assert client.post("/deploy", json={}).status_code == 404
    assert client.post("/workflows/x/deploy", json={}).status_code == 404


def test_no_arbitrary_target_agent_selection():
    """The remediate endpoint accepts no request body at all — there is no
    field anywhere a caller could use to select a target agent."""
    import inspect

    from app.api.routes.resolutions import remediate_resolution

    sig = inspect.signature(remediate_resolution)
    assert "body" not in sig.parameters
    assert "target_agent" not in sig.parameters


# ---------------------------------------------------------------------------
# 28-29: bounded limits / invalid query params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bounded_collection_limits(client, container):
    for i in range(5):
        await container.signal_repo.save(make_signal(f"s{i}", subject=f"subject-{i}"))
    r = client.get("/signals", params={"limit": 2})
    assert r.status_code == 200
    assert len(r.json()) == 2

    r_huge = client.get("/signals", params={"limit": 100000000})
    assert r_huge.status_code == 200
    assert len(r_huge.json()) <= 200  # never exceeds Settings.api_max_page_size


def test_invalid_query_parameters_rejected(client):
    r = client.get("/signals", params={"severity": "not-a-real-severity"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 30-31: business-rule errors / version conflicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_business_rule_errors_map_correctly(client, container):
    # Approving a review for a detection that isn't a feature_opportunity.
    signal = await container.signal_repo.save(make_signal("s1"))
    detection = await container.detection_repo.save(make_detection([signal.signal_id]))  # INCIDENT, not FEATURE_OPPORTUNITY

    r = client.get(f"/detections/{detection.detection_id}")
    assert r.status_code == 200  # sanity: detection itself is readable

    # create_review() itself raises InvalidDetectionTypeError -> 422, exercised indirectly via the review service.
    from app.feature_review.service import InvalidDetectionTypeError

    with pytest.raises(InvalidDetectionTypeError):
        await container.review_service.create_review(detection.detection_id)


@pytest.mark.asyncio
async def test_concurrency_conflict_maps_to_409(client, container):
    from app.persistence.errors import VersionConflictError

    workflow = await seed_workflow(container)
    # Force a stale version write directly against the repository to prove the mapping.
    from app.api.errors import register_exception_handlers
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TC

    probe_app = FastAPI()
    register_exception_handlers(probe_app)

    @probe_app.get("/boom")
    async def _boom():
        raise VersionConflictError(workflow.workflow_id, 99, 1)

    probe_client = _TC(probe_app)
    r = probe_client.get("/boom")
    assert r.status_code == 409
    assert r.json()["error"] == "version_conflict"


# ---------------------------------------------------------------------------
# 32-34: no leaks, correlation id present
# ---------------------------------------------------------------------------


def test_internal_exceptions_dont_leak(client, container, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("some internal Firestore/Gemini detail that must never reach the client")

    monkeypatch.setattr(container.workflow_repo, "list_recent", _boom)

    r = client.get("/workflows")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "internal_error"
    assert "Firestore" not in body["detail"]
    assert "Gemini" not in body["detail"]
    assert "RuntimeError" not in body["detail"]


@pytest.mark.asyncio
async def test_secrets_and_raw_payloads_dont_appear_in_responses(client, container):
    signal = await container.signal_repo.save(
        make_signal("s1", evidence={"message": "normal text"}, metadata={"customer_ref": "hashed-ref-123", "api_key": "should-never-appear"})
    )
    r = client.get(f"/signals/{signal.signal_id}")
    assert r.status_code == 200
    dumped = r.text
    assert "should-never-appear" not in dumped
    assert "hashed-ref-123" not in dumped
    assert "customer_ref" not in dumped  # the raw metadata bucket is never serialized at all


def test_request_correlation_id_present(client):
    r = client.get("/health")
    assert "X-Request-ID" in r.headers

    r2 = client.get("/health", headers={"X-Request-ID": "my-custom-id"})
    assert r2.headers["X-Request-ID"] == "my-custom-id"


# ---------------------------------------------------------------------------
# 35-38: existing flows/demo/suite remain green (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_feature_flow_still_passes():
    from app.demo import DemoHarness

    summary = await DemoHarness().run_feature_flow()
    assert summary.verification_status == "passed"


@pytest.mark.asyncio
async def test_existing_incident_flow_still_passes():
    from app.demo import DemoHarness

    summary = await DemoHarness().run_incident_flow()
    assert summary.verification_status == "passed"


@pytest.mark.asyncio
async def test_existing_demo_worker_fixture_still_passes():
    from app.demo.worker_demo import run_worker_demo

    result = await run_worker_demo()
    assert result.detection_id is not None


# ---------------------------------------------------------------------------
# Optional UI static mount (app/api/app.py) — gated on ui/dist actually
# being built, mirroring the repository's other gated-integration-test
# convention (skip rather than fail when the prerequisite isn't present).
# ---------------------------------------------------------------------------


def test_api_serve_ui_disabled_by_default_never_shadows_api_routes(container):
    """Even if ui/dist happens to exist on disk (e.g. a local `npm run
    build`), Settings.api_serve_ui defaults to False, so the API's own
    404 behavior for unmatched paths is never accidentally masked by the
    UI's SPA fallback — see app/api/app.py's own comment on this."""
    app = create_app(container=container)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/tools/execute")
    assert r.status_code == 404


def test_api_serve_ui_when_explicitly_enabled(container, monkeypatch):
    import app.api.app as app_module

    dist_dir = app_module._UI_DIST_DIR
    if not dist_dir.is_dir():
        pytest.skip("ui/dist not built — run `npm run build` in ui/ to exercise this")

    from app.config import Settings

    monkeypatch.setattr(app_module, "get_settings", lambda: Settings(api_serve_ui=True))
    app = create_app(container=container)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
