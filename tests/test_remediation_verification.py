"""Tests for post-remediation production verification (app/verification/).
Uses real in-memory repositories throughout — no mocked repository stands
in for actual persistence/query behavior. See
docs/architecture/remediation_verification.md.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.domain import (
    Artifact,
    ArtifactType,
    DetectionDomain,
    DetectionType,
    RemediationRisk,
    RemediationStrategy,
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
    compute_detection_fingerprint,
    compute_fingerprint,
    compute_resolution_fingerprint,
)
from app.domain.detection import DetectionResult
from app.persistence.errors import VersionConflictError
from app.persistence.memory.repositories import (
    InMemoryArtifactRepository,
    InMemoryDetectionRepository,
    InMemoryRemediationVerificationRepository,
    InMemoryResolutionRepository,
    InMemorySignalRepository,
    InMemoryWorkflowRepository,
)
from app.persistence.repositories.remediation_verification import RemediationVerificationQuery
from app.verification.errors import VerificationError
from app.verification.service import RemediationVerificationService

NOW = datetime.now(timezone.utc)
SERVICE = "checkout"
ENV = "production"
REVISION = "rev-remediated"


def make_signal(source_event_id: str, *, stype: SignalType, severity=SignalSeverity.WARNING, service=SERVICE, env=ENV, revision=None, minutes_ago=1, evidence=None, subject=None) -> Signal:
    subject = subject or service
    return Signal(
        signal_type=stype,
        source=SignalSource.CLOUD_MONITORING,
        severity=severity,
        observed_at=NOW - timedelta(minutes=minutes_ago),
        subject=subject,
        summary=f"signal {source_event_id}",
        service_name=service,
        environment=env,
        revision=revision,
        evidence=evidence or {},
        provenance=SignalProvenance(source_system="x", source_event_id=source_event_id),
        fingerprint=compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id=source_event_id, subject=subject),
    )


class Harness:
    def __init__(self):
        self.resolution_repo = InMemoryResolutionRepository()
        self.detection_repo = InMemoryDetectionRepository()
        self.workflow_repo = InMemoryWorkflowRepository()
        self.artifact_repo = InMemoryArtifactRepository()
        self.signal_repo = InMemorySignalRepository()
        self.verification_repo = InMemoryRemediationVerificationRepository()
        self.service = RemediationVerificationService(
            resolution_repo=self.resolution_repo,
            detection_repo=self.detection_repo,
            workflow_repo=self.workflow_repo,
            artifact_repo=self.artifact_repo,
            signal_repo=self.signal_repo,
            verification_repo=self.verification_repo,
        )

    async def seed_incident(self, *, baseline_signals: list[Signal], deployment_status="succeeded", revision=REVISION, artifact_created_at=None):
        for s in baseline_signals:
            await self.signal_repo.save(s)
        baseline_ids = [s.signal_id for s in baseline_signals]

        detection = DetectionResult(
            detection_type=DetectionType.INCIDENT,
            domain=DetectionDomain.OPERATIONAL,
            title="Elevated errors",
            summary="Elevated errors observed",
            rationale="Errors clustered after deployment",
            confidence=0.9,
            severity=SignalSeverity.CRITICAL,
            subject=SERVICE,
            service_name=SERVICE,
            environment=ENV,
            supporting_signal_ids=baseline_ids,
            observation_window_minutes=15,
            fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject=SERVICE, supporting_signal_ids=baseline_ids, window_minutes=15),
        )
        await self.detection_repo.save(detection)

        workflow = WorkflowState(ticket=Ticket(title="t", description="d"), current_stage=WorkflowStage.COMPLETED)
        await self.workflow_repo.create(workflow)

        resolution = ResolutionResult(
            detection_id=detection.detection_id,
            workflow_id=workflow.workflow_id,
            diagnosis_summary="root cause",
            probable_root_cause="bad deploy",
            root_cause_confidence=0.85,
            remediation_strategy=RemediationStrategy.CODE_FIX,
            remediation_rationale="fix it",
            expected_outcome="errors clear",
            verification_strategy="monitor",
            risk=RemediationRisk.LOW,
            target_agent="codegen_agent",
            supporting_signal_ids=baseline_ids,
            fingerprint=compute_resolution_fingerprint(detection_id=detection.detection_id, remediation_strategy=RemediationStrategy.CODE_FIX, subject=SERVICE),
        )
        await self.resolution_repo.save(resolution)

        artifact = Artifact(
            artifact_type=ArtifactType.DEPLOYMENT,
            created_by="deployment_agent",
            created_at=artifact_created_at or NOW,
            payload={"status": deployment_status, "revision": revision, "service_name": SERVICE, "environment": ENV},
        )
        await self.artifact_repo.save(workflow.workflow_id, artifact)
        workflow = await self.workflow_repo.update_if_version(workflow.workflow_id, workflow.version, workflow.model_copy(update={"artifact_ids": [artifact.artifact_id]}))

        return resolution, detection, workflow, artifact


def make_post_signal(source_event_id: str, *, stype, severity=SignalSeverity.INFO, revision=REVISION, minutes_ago=0, evidence=None) -> Signal:
    # minutes_ago=0 (observed at/after the deployment's own created_at=NOW,
    # both frozen to the same module-level NOW) so it falls inside the
    # verification window's [deployed_at, deployed_at+window] range.
    return make_signal(source_event_id, stype=stype, severity=severity, revision=revision, minutes_ago=minutes_ago, evidence=evidence)


# ---------------------------------------------------------------------------
# 1-2: deployment success != resolution; healthy evidence -> VERIFIED_RESOLVED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_deployment_does_not_equal_resolution():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, workflow, _ = await h.seed_incident(baseline_signals=baseline)
    # No post-deployment evidence collected yet — deployment "succeeded" alone must not resolve anything.
    result = await h.service.verify_remediation(resolution.resolution_id)
    assert result.outcome != VerificationOutcome.VERIFIED_RESOLVED
    assert result.outcome == VerificationOutcome.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_healthy_post_deployment_evidence_verifies_resolved():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, workflow, artifact = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO))

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.outcome == VerificationOutcome.VERIFIED_RESOLVED
    assert result.status == VerificationStatus.COMPLETED
    workflow_after = await h.workflow_repo.get(workflow.workflow_id)
    assert workflow_after.metadata["remediation_outcome"] == "verified_resolved"


# ---------------------------------------------------------------------------
# 3-4: continued degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continued_latency_degradation_still_degraded():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.LATENCY_ANOMALY, severity=SignalSeverity.INFO, evidence={"value": 900})]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.LATENCY_ANOMALY, severity=SignalSeverity.INFO, evidence={"value": 950}))

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.outcome == VerificationOutcome.STILL_DEGRADED
    assert "latency_anomaly" in result.reason


@pytest.mark.asyncio
async def test_continued_application_errors_still_degraded():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.ERROR))

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.outcome == VerificationOutcome.STILL_DEGRADED


# ---------------------------------------------------------------------------
# 5-6: zero/insufficient evidence never resolves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_post_deployment_evidence_insufficient():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.outcome == VerificationOutcome.INSUFFICIENT_EVIDENCE
    assert result.outcome != VerificationOutcome.VERIFIED_RESOLVED


@pytest.mark.asyncio
async def test_insufficient_evidence_never_resolves_incident():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.METRIC_ANOMALY, severity=SignalSeverity.CRITICAL), make_signal("base-2", stype=SignalType.LATENCY_ANOMALY, severity=SignalSeverity.INFO, evidence={"value": 900})]
    resolution, _, workflow, _ = await h.seed_incident(baseline_signals=baseline)
    # Only ONE of the two original condition types gets post-deployment evidence.
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.METRIC_ANOMALY, severity=SignalSeverity.INFO))

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.outcome == VerificationOutcome.INSUFFICIENT_EVIDENCE
    workflow_after = await h.workflow_repo.get(workflow.workflow_id)
    assert workflow_after.metadata["remediation_outcome"] != "verified_resolved"


# ---------------------------------------------------------------------------
# 7-10: correlation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrelated_service_signals_cannot_verify():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_signal("post-other-service", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO, service="billing", revision=REVISION, minutes_ago=0))

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.outcome == VerificationOutcome.INSUFFICIENT_EVIDENCE  # the unrelated-service signal was never counted


@pytest.mark.asyncio
async def test_unrelated_revision_signals_cannot_verify():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-other-rev", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO, revision="rev-unrelated"))

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.outcome == VerificationOutcome.INSUFFICIENT_EVIDENCE  # the wrong-revision signal was excluded


@pytest.mark.asyncio
async def test_deployment_revision_correlation_works():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, artifact = await h.seed_incident(baseline_signals=baseline, revision="rev-42")
    await h.signal_repo.save(make_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO, revision="rev-42", minutes_ago=0))
    await h.signal_repo.save(make_signal("post-wrong", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL, revision="rev-other", minutes_ago=0))

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.revision == "rev-42"
    assert result.outcome == VerificationOutcome.VERIFIED_RESOLVED  # only the matching-revision signal counted, and it's healthy


@pytest.mark.asyncio
async def test_deployment_artifact_correlation_works():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, artifact = await h.seed_incident(baseline_signals=baseline)

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.deployment_artifact_id == artifact.artifact_id


# ---------------------------------------------------------------------------
# 11-13: evidence stored by reference, not raw payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_evidence_retained_by_reference():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, detection, _, _ = await h.seed_incident(baseline_signals=baseline)

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.baseline_detection_id == detection.detection_id
    assert result.baseline_signal_ids == [s.signal_id for s in baseline]


@pytest.mark.asyncio
async def test_post_deployment_evidence_retained_by_reference():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    post = await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO))

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert post.signal_id in result.post_deployment_signal_ids
    assert post.signal_id in result.supporting_signal_ids


@pytest.mark.asyncio
async def test_raw_monitoring_payload_not_persisted():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL, evidence={"message": "SECRET-RAW-LOG-TEXT"})]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO, evidence={"message": "ANOTHER-SECRET"}))

    result = await h.service.verify_remediation(resolution.resolution_id)

    dumped = result.model_dump_json()
    assert "SECRET-RAW-LOG-TEXT" not in dumped
    assert "ANOTHER-SECRET" not in dumped


# ---------------------------------------------------------------------------
# 14-15: idempotency / concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verification_is_idempotent():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO))

    first = await h.service.verify_remediation(resolution.resolution_id)
    second = await h.service.verify_remediation(resolution.resolution_id)

    assert first.verification_id == second.verification_id
    all_records = await h.verification_repo.query(RemediationVerificationQuery(limit=500))
    assert len(all_records) == 1


@pytest.mark.asyncio
async def test_concurrent_verification_attempts_are_safe():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO))

    results = await asyncio.gather(
        h.service.verify_remediation(resolution.resolution_id),
        h.service.verify_remediation(resolution.resolution_id),
        h.service.verify_remediation(resolution.resolution_id),
    )

    ids = {r.verification_id for r in results}
    assert len(ids) == 1
    all_records = await h.verification_repo.query(RemediationVerificationQuery(limit=500))
    assert len(all_records) == 1


# ---------------------------------------------------------------------------
# 16: monitoring failure never falsely resolves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_deployment_artifact_escalates_not_resolves():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline, deployment_status="failed")

    result = await h.service.verify_remediation(resolution.resolution_id)

    assert result.outcome == VerificationOutcome.ESCALATED
    assert result.outcome != VerificationOutcome.VERIFIED_RESOLVED


# ---------------------------------------------------------------------------
# 17-19: safety boundary
# ---------------------------------------------------------------------------


def test_verification_service_has_no_deploy_or_rollback_surface():
    methods = {name for name in dir(RemediationVerificationService) if not name.startswith("_")}
    assert methods == {"verify_remediation"}


@pytest.mark.asyncio
async def test_verification_never_changes_remediation_strategy():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO))

    await h.service.verify_remediation(resolution.resolution_id)

    resolution_after = await h.resolution_repo.get(resolution.resolution_id)
    assert resolution_after.remediation_strategy == RemediationStrategy.CODE_FIX  # byte-identical, untouched


@pytest.mark.asyncio
async def test_verification_ignores_target_agent_entirely():
    """RemediationVerificationService never reads resolution.target_agent
    at all — it correlates purely via the workflow's own deployment
    artifact, never by trusting a model-supplied routing field."""
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)
    await h.signal_repo.save(make_post_signal("post-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.INFO))

    import inspect

    source = inspect.getsource(RemediationVerificationService)
    assert "target_agent" not in source


# ---------------------------------------------------------------------------
# Structural / adversarial errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_resolution_id_raises():
    h = Harness()
    with pytest.raises(VerificationError):
        await h.service.verify_remediation("does-not-exist")


@pytest.mark.asyncio
async def test_no_deployment_artifact_raises():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, workflow, _ = await h.seed_incident(baseline_signals=baseline)
    # Strip the deployment artifact reference to simulate "not deployed yet".
    workflow_now = await h.workflow_repo.get(workflow.workflow_id)
    await h.workflow_repo.update_if_version(workflow_now.workflow_id, workflow_now.version, workflow_now.model_copy(update={"artifact_ids": []}))

    with pytest.raises(VerificationError):
        await h.service.verify_remediation(resolution.resolution_id)


# ---------------------------------------------------------------------------
# 16: a monitoring/query-level failure never falsely resolves the incident
# ---------------------------------------------------------------------------


class _FailingSignalRepo(InMemorySignalRepository):
    async def query(self, query):
        raise ConnectionError("monitoring backend temporarily unavailable")


@pytest.mark.asyncio
async def test_monitoring_query_failure_never_falsely_resolves():
    h = Harness()
    baseline = [make_signal("base-1", stype=SignalType.APPLICATION_ERROR, severity=SignalSeverity.CRITICAL)]
    resolution, _, _, _ = await h.seed_incident(baseline_signals=baseline)

    # Swap in a signal repo whose query() fails, matching a real Monitoring
    # collection outage — the service must raise, never silently persist a
    # VERIFIED_RESOLVED record from a failed collection attempt. get() must
    # still work (it's how the baseline signal types are resolved), so the
    # baseline signal is re-saved into this repo too.
    failing_signals = _FailingSignalRepo()
    for signal in baseline:
        await failing_signals.save(signal)
    h.service = RemediationVerificationService(
        resolution_repo=h.resolution_repo,
        detection_repo=h.detection_repo,
        workflow_repo=h.workflow_repo,
        artifact_repo=h.artifact_repo,
        signal_repo=failing_signals,
        verification_repo=h.verification_repo,
    )
    # The baseline lookup itself uses signal_repo.get(), which _FailingSignalRepo doesn't override — only query() fails (the post-deployment collection step).
    with pytest.raises(ConnectionError):
        await h.service.verify_remediation(resolution.resolution_id)

    all_records = await h.verification_repo.query(RemediationVerificationQuery(limit=500))
    assert all(v.outcome != VerificationOutcome.VERIFIED_RESOLVED for v in all_records)


# ---------------------------------------------------------------------------
# 20-22: existing flows remain green (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_incident_remediation_flow_still_passes():
    from app.demo import DemoHarness

    summary = await DemoHarness().run_incident_flow()
    assert summary.verification_status == "passed"


@pytest.mark.asyncio
async def test_existing_feature_discovery_flow_still_passes():
    from app.demo import DemoHarness

    summary = await DemoHarness().run_feature_flow()
    assert summary.verification_status == "passed"
