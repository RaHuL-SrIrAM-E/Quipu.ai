"""RemediationVerificationService — the deterministic, non-agent component
that decides whether AI-generated remediation actually fixed production.
Not a QuipuAgent (same "not an agent" precedent as
app.feature_review.service.FeatureReviewService/
app.orchestration.service.OrchestrationService — a deterministic
application/infrastructure service, zero LLM calls, direct repository
injection rather than the agent-facing gateway layer).

    ResolutionResult (authorized remediation)
        -> WorkflowState (reopened, now COMPLETED again after remediation)
        -> latest DEPLOYMENT Artifact (revision/service_name/environment)
        -> SignalRepository query, scoped to that revision/service/window
        -> app.verification.policy (deterministic comparison)
        -> RemediationVerification, persisted via RemediationVerificationRepository

See docs/architecture/remediation_verification.md for the full design and
docs/architecture/incident_remediation.md for how this connects to the
existing "deployed_pending_verification" marker
(app.orchestration.service._execute_decision).
"""

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.core.observability import get_logger
from app.domain import (
    ArtifactType,
    DetectionType,
    RemediationVerification,
    VerificationOutcome,
    VerificationStatus,
    compute_verification_key,
)
from app.persistence.errors import DuplicateEntityError, VersionConflictError
from app.persistence.repositories.artifact import ArtifactRepository
from app.persistence.repositories.detection import DetectionRepository
from app.persistence.repositories.remediation_verification import RemediationVerificationRepository
from app.persistence.repositories.resolution import ResolutionRepository
from app.persistence.repositories.signal import SignalRepository
from app.persistence.repositories.workflow import WorkflowRepository
from app.verification.errors import VerificationError
from app.verification.policy import VERIFIABLE_SIGNAL_TYPES, collect_post_deployment_signals, decide_outcome, evaluate_condition

logger = get_logger("quipu.verification.service")


class RemediationVerificationService:
    def __init__(
        self,
        *,
        resolution_repo: ResolutionRepository,
        detection_repo: DetectionRepository,
        workflow_repo: WorkflowRepository,
        artifact_repo: ArtifactRepository,
        signal_repo: SignalRepository,
        verification_repo: RemediationVerificationRepository,
    ):
        self._resolutions = resolution_repo
        self._detections = detection_repo
        self._workflows = workflow_repo
        self._artifacts = artifact_repo
        self._signals = signal_repo
        self._verifications = verification_repo

    async def verify_remediation(self, resolution_id: str) -> RemediationVerification:
        resolution = await self._resolutions.get(resolution_id)
        if resolution is None:
            raise VerificationError(f"ResolutionResult '{resolution_id}' not found")

        detection = await self._detections.get(resolution.detection_id)
        if detection is None or detection.detection_type != DetectionType.INCIDENT:
            raise VerificationError(f"ResolutionResult '{resolution_id}' is not associated with a valid INCIDENT DetectionResult")

        if resolution.workflow_id is None:
            raise VerificationError(f"ResolutionResult '{resolution_id}' has no associated workflow_id — nothing to verify")
        workflow = await self._workflows.get(resolution.workflow_id)
        if workflow is None:
            raise VerificationError(f"workflow '{resolution.workflow_id}' referenced by ResolutionResult '{resolution_id}' not found")

        deployment_artifact = await self._find_latest_deployment_artifact(workflow)
        if deployment_artifact is None:
            raise VerificationError(f"workflow '{workflow.workflow_id}' has no DEPLOYMENT artifact yet — remediation has not deployed")

        settings = get_settings()
        window_minutes = settings.verification_window_minutes
        revision = deployment_artifact.payload.get("revision")
        service_name = deployment_artifact.payload.get("service_name")
        environment = deployment_artifact.payload.get("environment")

        idempotency_key = compute_verification_key(
            resolution_id=resolution_id, deployment_artifact_id=deployment_artifact.artifact_id, revision=revision, window_minutes=window_minutes
        )
        existing = await self._verifications.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            logger.info("verification.idempotent_hit resolution_id=%s verification_id=%s outcome=%s", resolution_id, existing.verification_id, existing.outcome)
            return existing

        baseline_signal_ids = list(detection.supporting_signal_ids)
        baseline_summary = await self._build_baseline_summary(baseline_signal_ids)

        record = RemediationVerification(
            verification_id=idempotency_key,
            resolution_id=resolution_id,
            workflow_id=workflow.workflow_id,
            deployment_artifact_id=deployment_artifact.artifact_id,
            revision=revision,
            baseline_detection_id=detection.detection_id,
            baseline_signal_ids=baseline_signal_ids,
            baseline_summary=baseline_summary,
            idempotency_key=idempotency_key,
            status=VerificationStatus.IN_PROGRESS,
        )
        try:
            record = await self._verifications.create(record)
        except DuplicateEntityError:
            # Lost the race to a concurrent verify_remediation() call for
            # the exact same deployment — never do the work twice. See
            # docs/architecture/remediation_verification.md "Idempotency".
            current = await self._verifications.get(idempotency_key)
            if current is not None:
                logger.info("verification.concurrent_hit resolution_id=%s verification_id=%s", resolution_id, idempotency_key)
                return current
            raise

        await self._mirror_status_onto_workflow(workflow, "verification_in_progress")
        started = datetime.now(timezone.utc)

        if deployment_artifact.payload.get("status") != "succeeded":
            finalized = record.model_copy(
                update={
                    "status": VerificationStatus.COMPLETED,
                    "outcome": VerificationOutcome.ESCALATED,
                    "reason": "the correlated deployment artifact does not indicate a successful deployment — verification cannot proceed safely",
                    "verification_completed_at": datetime.now(timezone.utc),
                }
            )
            saved = await self._verifications.update_if_version(record.verification_id, record.version, finalized)
            await self._mirror_outcome_onto_workflow(workflow, saved)
            self._log_outcome(resolution_id, saved, evidence_count=0, duration_ms=self._duration_ms(started))
            return saved

        baseline_signal_types = await self._baseline_signal_types(baseline_signal_ids)
        condition_types = baseline_signal_types & VERIFIABLE_SIGNAL_TYPES

        deployed_at = deployment_artifact.created_at if deployment_artifact.created_at.tzinfo else deployment_artifact.created_at.replace(tzinfo=timezone.utc)
        until = deployed_at + timedelta(minutes=window_minutes)

        post_signals_by_type = await collect_post_deployment_signals(
            self._signals,
            condition_types=condition_types,
            service_name=service_name,
            environment=environment,
            revision=revision,
            since=deployed_at,
            until=until,
            max_signals=settings.verification_max_signals_per_condition,
        )

        evaluations = [evaluate_condition(signal_type, signals) for signal_type, signals in post_signals_by_type.items()]
        all_post_signal_ids = sorted({s.signal_id for signals in post_signals_by_type.values() for s in signals})
        supporting_signal_ids = sorted({sid for e in evaluations for sid in e.matched_signal_ids})

        outcome, reason = decide_outcome(
            evaluations, total_post_deployment_signals=len(all_post_signal_ids), minimum_post_deployment_signals=settings.verification_minimum_post_deployment_signals
        )

        finalized = record.model_copy(
            update={
                "status": VerificationStatus.COMPLETED,
                "outcome": outcome,
                "reason": reason,
                "post_deployment_signal_ids": all_post_signal_ids,
                "supporting_signal_ids": supporting_signal_ids,
                "evidence_summary": {e.signal_type.value: e.verdict for e in evaluations},
                "verification_completed_at": datetime.now(timezone.utc),
            }
        )
        saved = await self._verifications.update_if_version(record.verification_id, record.version, finalized)
        await self._mirror_outcome_onto_workflow(workflow, saved)
        self._log_outcome(resolution_id, saved, evidence_count=len(all_post_signal_ids), duration_ms=self._duration_ms(started))
        return saved

    async def _find_latest_deployment_artifact(self, workflow):
        for artifact_id in reversed(workflow.artifact_ids):
            artifact = await self._artifacts.get(workflow.workflow_id, artifact_id)
            if artifact is not None and artifact.artifact_type == ArtifactType.DEPLOYMENT:
                return artifact
        return None

    async def _baseline_signal_types(self, signal_ids: list[str]) -> set:
        types = set()
        for signal_id in signal_ids:
            signal = await self._signals.get(signal_id)
            if signal is not None:
                types.add(signal.signal_type)
        return types

    async def _build_baseline_summary(self, signal_ids: list[str]) -> str:
        parts = []
        for signal_id in signal_ids:
            signal = await self._signals.get(signal_id)
            if signal is not None:
                parts.append(f"{signal.signal_type.value}({signal.severity.value})")
        if not parts:
            return f"{len(signal_ids)} signal(s), none resolvable"
        return f"{len(signal_ids)} signal(s): " + ", ".join(parts)

    async def _mirror_outcome_onto_workflow(self, workflow, verification: RemediationVerification) -> None:
        await self._mirror_status_onto_workflow(workflow, verification.outcome.value, verification_id=verification.verification_id)

    async def _mirror_status_onto_workflow(self, workflow, remediation_outcome: str, *, verification_id: str | None = None) -> None:
        """Additive metadata mirror only — WorkflowState.status/current_stage
        (the real state machine) are never touched here; see
        docs/architecture/remediation_verification.md §11 "Incident
        lifecycle" for why this reuses the existing `remediation_outcome`
        metadata marker (app.orchestration.service._execute_decision)
        instead of inventing a second state machine. Best-effort: a lost
        version race here never fails verification — the
        RemediationVerification record itself (already durably saved) is
        the authoritative result; this is only a convenience mirror for
        anything reading WorkflowState directly."""
        current = await self._workflows.get(workflow.workflow_id)
        if current is None:
            return
        metadata = {**current.metadata, "remediation_outcome": remediation_outcome}
        if verification_id is not None:
            metadata["latest_verification_id"] = verification_id
        try:
            await self._workflows.update_if_version(current.workflow_id, current.version, current.model_copy(update={"metadata": metadata}))
        except VersionConflictError:
            logger.warning("verification: lost a concurrent write race mirroring remediation_outcome onto workflow %s — verification record remains authoritative", workflow.workflow_id)

    def _duration_ms(self, started: datetime) -> float:
        return (datetime.now(timezone.utc) - started).total_seconds() * 1000

    def _log_outcome(self, resolution_id: str, verification: RemediationVerification, *, evidence_count: int, duration_ms: float) -> None:
        logger.info(
            "verification.completed resolution_id=%s workflow_id=%s deployment_artifact_id=%s revision=%s verification_id=%s outcome=%s evidence_count=%d duration_ms=%.1f",
            resolution_id,
            verification.workflow_id,
            verification.deployment_artifact_id,
            verification.revision,
            verification.verification_id,
            verification.outcome.value if verification.outcome else None,
            evidence_count,
            duration_ms,
        )
