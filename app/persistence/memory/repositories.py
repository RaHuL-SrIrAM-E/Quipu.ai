"""In-memory repository implementations — deterministic, no external
dependency. Used for unit tests and local development before Firestore is
configured. InMemoryWorkflowRepository enforces the same version-conflict
semantics as FirestoreWorkflowRepository, so tests exercise real concurrency
behaviour without needing a live Firestore connection.
"""

from app.domain import AgentExecution, Artifact, Decision, DetectionResult, FeatureReview, RemediationVerification, ResolutionResult, Signal, WorkflowState
from app.persistence.errors import DuplicateEntityError, EntityNotFoundError, VersionConflictError
from app.persistence.repositories.detection import DetectionQuery
from app.persistence.repositories.feature_review import FeatureReviewQuery
from app.persistence.repositories.incident import IncidentRecord
from app.persistence.repositories.remediation_verification import RemediationVerificationQuery
from app.persistence.repositories.resolution import ResolutionQuery
from app.persistence.repositories.signal import SignalQuery


class InMemoryWorkflowRepository:
    def __init__(self):
        self._store: dict[str, WorkflowState] = {}

    async def create(self, workflow: WorkflowState) -> WorkflowState:
        if workflow.workflow_id in self._store:
            raise DuplicateEntityError("WorkflowState", workflow.workflow_id)
        self._store[workflow.workflow_id] = workflow.model_copy(deep=True)
        return workflow.model_copy(deep=True)

    async def get(self, workflow_id: str) -> WorkflowState | None:
        stored = self._store.get(workflow_id)
        return stored.model_copy(deep=True) if stored else None

    async def update(self, workflow: WorkflowState) -> WorkflowState:
        if workflow.workflow_id not in self._store:
            raise EntityNotFoundError("WorkflowState", workflow.workflow_id)
        self._store[workflow.workflow_id] = workflow.model_copy(deep=True)
        return workflow.model_copy(deep=True)

    async def delete(self, workflow_id: str) -> None:
        if workflow_id not in self._store:
            raise EntityNotFoundError("WorkflowState", workflow_id)
        del self._store[workflow_id]

    async def update_if_version(
        self, workflow_id: str, expected_version: int, updated_workflow: WorkflowState
    ) -> WorkflowState:
        current = self._store.get(workflow_id)
        if current is None:
            raise EntityNotFoundError("WorkflowState", workflow_id)
        if current.version != expected_version:
            raise VersionConflictError(workflow_id, expected_version, current.version)
        new_state = updated_workflow.model_copy(update={"version": expected_version + 1})
        self._store[workflow_id] = new_state.model_copy(deep=True)
        return new_state.model_copy(deep=True)

    async def list_recent(self, *, status=None, limit: int = 50) -> list[WorkflowState]:
        results = [w.model_copy(deep=True) for w in self._store.values() if status is None or w.status == status]
        return results[-limit:][::-1]  # most-recently-inserted first, bounded


class InMemoryArtifactRepository:
    def __init__(self):
        self._store: dict[str, dict[str, Artifact]] = {}

    async def save(self, workflow_id: str, artifact: Artifact) -> Artifact:
        self._store.setdefault(workflow_id, {})[artifact.artifact_id] = artifact.model_copy(deep=True)
        return artifact.model_copy(deep=True)

    async def get(self, workflow_id: str, artifact_id: str) -> Artifact | None:
        artifact = self._store.get(workflow_id, {}).get(artifact_id)
        return artifact.model_copy(deep=True) if artifact else None

    async def list_for_workflow(self, workflow_id: str) -> list[Artifact]:
        return [a.model_copy(deep=True) for a in self._store.get(workflow_id, {}).values()]

    async def find_owning_workflow_id(self, artifact_id: str) -> str | None:
        for workflow_id, artifacts in self._store.items():
            if artifact_id in artifacts:
                return workflow_id
        return None


class InMemoryAgentExecutionRepository:
    def __init__(self):
        self._store: dict[str, dict[str, AgentExecution]] = {}

    async def create(self, execution: AgentExecution) -> AgentExecution:
        bucket = self._store.setdefault(execution.workflow_id, {})
        if execution.execution_id in bucket:
            raise DuplicateEntityError("AgentExecution", execution.execution_id)
        bucket[execution.execution_id] = execution.model_copy(deep=True)
        return execution.model_copy(deep=True)

    async def get(self, workflow_id: str, execution_id: str) -> AgentExecution | None:
        execution = self._store.get(workflow_id, {}).get(execution_id)
        return execution.model_copy(deep=True) if execution else None

    async def list_for_workflow(self, workflow_id: str) -> list[AgentExecution]:
        return [e.model_copy(deep=True) for e in self._store.get(workflow_id, {}).values()]

    async def update(self, execution: AgentExecution) -> AgentExecution:
        bucket = self._store.get(execution.workflow_id, {})
        if execution.execution_id not in bucket:
            raise EntityNotFoundError("AgentExecution", execution.execution_id)
        bucket[execution.execution_id] = execution.model_copy(deep=True)
        return execution.model_copy(deep=True)


class InMemoryDecisionRepository:
    def __init__(self):
        self._store: dict[str, dict[str, Decision]] = {}

    async def save(self, workflow_id: str, decision: Decision) -> Decision:
        self._store.setdefault(workflow_id, {})[decision.decision_id] = decision.model_copy(deep=True)
        return decision.model_copy(deep=True)

    async def get(self, workflow_id: str, decision_id: str) -> Decision | None:
        decision = self._store.get(workflow_id, {}).get(decision_id)
        return decision.model_copy(deep=True) if decision else None

    async def list_for_workflow(self, workflow_id: str) -> list[Decision]:
        return [d.model_copy(deep=True) for d in self._store.get(workflow_id, {}).values()]


class InMemoryIncidentRepository:
    def __init__(self):
        self._store: dict[str, dict[str, IncidentRecord]] = {}

    async def save(self, incident: IncidentRecord) -> IncidentRecord:
        self._store.setdefault(incident.workflow_id, {})[incident.incident_id] = incident.model_copy(deep=True)
        return incident.model_copy(deep=True)

    async def get(self, workflow_id: str, incident_id: str) -> IncidentRecord | None:
        incident = self._store.get(workflow_id, {}).get(incident_id)
        return incident.model_copy(deep=True) if incident else None

    async def list_for_workflow(self, workflow_id: str) -> list[IncidentRecord]:
        return [i.model_copy(deep=True) for i in self._store.get(workflow_id, {}).values()]


class InMemorySignalRepository:
    def __init__(self):
        self._store: dict[str, Signal] = {}
        self._by_fingerprint: dict[str, str] = {}  # fingerprint -> signal_id

    async def save(self, signal: Signal) -> Signal:
        self._store[signal.signal_id] = signal.model_copy(deep=True)
        self._by_fingerprint[signal.fingerprint] = signal.signal_id
        return signal.model_copy(deep=True)

    async def get(self, signal_id: str) -> Signal | None:
        signal = self._store.get(signal_id)
        return signal.model_copy(deep=True) if signal else None

    async def find_by_fingerprint(self, fingerprint: str) -> Signal | None:
        signal_id = self._by_fingerprint.get(fingerprint)
        if signal_id is None:
            return None
        return await self.get(signal_id)

    async def query(self, query: SignalQuery) -> list[Signal]:
        results = []
        for signal in self._store.values():
            if query.signal_type is not None and signal.signal_type != query.signal_type:
                continue
            if query.source is not None and signal.source != query.source:
                continue
            if query.service_name is not None and signal.service_name != query.service_name:
                continue
            if query.environment is not None and signal.environment != query.environment:
                continue
            if query.severity is not None and signal.severity != query.severity:
                continue
            if query.status is not None and signal.status != query.status:
                continue
            if query.since is not None and signal.observed_at < query.since:
                continue
            if query.until is not None and signal.observed_at > query.until:
                continue
            results.append(signal.model_copy(deep=True))
        results.sort(key=lambda s: s.observed_at, reverse=True)
        return results[: query.limit]


class InMemoryDetectionRepository:
    def __init__(self):
        self._store: dict[str, DetectionResult] = {}
        self._by_fingerprint: dict[str, str] = {}  # fingerprint -> detection_id

    async def save(self, detection: DetectionResult) -> DetectionResult:
        self._store[detection.detection_id] = detection.model_copy(deep=True)
        self._by_fingerprint[detection.fingerprint] = detection.detection_id
        return detection.model_copy(deep=True)

    async def get(self, detection_id: str) -> DetectionResult | None:
        detection = self._store.get(detection_id)
        return detection.model_copy(deep=True) if detection else None

    async def find_by_fingerprint(self, fingerprint: str) -> DetectionResult | None:
        detection_id = self._by_fingerprint.get(fingerprint)
        if detection_id is None:
            return None
        return await self.get(detection_id)

    async def query(self, query: DetectionQuery) -> list[DetectionResult]:
        results = []
        for detection in self._store.values():
            if query.detection_type is not None and detection.detection_type != query.detection_type:
                continue
            if query.domain is not None and detection.domain != query.domain:
                continue
            if query.service_name is not None and detection.service_name != query.service_name:
                continue
            if query.environment is not None and detection.environment != query.environment:
                continue
            if query.since is not None and detection.detected_at < query.since:
                continue
            if query.until is not None and detection.detected_at > query.until:
                continue
            results.append(detection.model_copy(deep=True))
        results.sort(key=lambda d: d.detected_at, reverse=True)
        return results[: query.limit]


class InMemoryResolutionRepository:
    def __init__(self):
        self._store: dict[str, ResolutionResult] = {}
        self._by_fingerprint: dict[str, str] = {}  # fingerprint -> resolution_id

    async def save(self, resolution: ResolutionResult) -> ResolutionResult:
        self._store[resolution.resolution_id] = resolution.model_copy(deep=True)
        self._by_fingerprint[resolution.fingerprint] = resolution.resolution_id
        return resolution.model_copy(deep=True)

    async def get(self, resolution_id: str) -> ResolutionResult | None:
        resolution = self._store.get(resolution_id)
        return resolution.model_copy(deep=True) if resolution else None

    async def find_by_fingerprint(self, fingerprint: str) -> ResolutionResult | None:
        resolution_id = self._by_fingerprint.get(fingerprint)
        if resolution_id is None:
            return None
        return await self.get(resolution_id)

    async def query(self, query: ResolutionQuery) -> list[ResolutionResult]:
        results = []
        for resolution in self._store.values():
            if query.detection_id is not None and resolution.detection_id != query.detection_id:
                continue
            if query.remediation_strategy is not None and resolution.remediation_strategy != query.remediation_strategy:
                continue
            if query.risk is not None and resolution.risk != query.risk:
                continue
            if query.since is not None and resolution.resolved_at < query.since:
                continue
            if query.until is not None and resolution.resolved_at > query.until:
                continue
            results.append(resolution.model_copy(deep=True))
        results.sort(key=lambda r: r.resolved_at, reverse=True)
        return results[: query.limit]


class InMemoryFeatureReviewRepository:
    def __init__(self):
        self._store: dict[str, FeatureReview] = {}
        self._by_detection_id: dict[str, str] = {}  # detection_id -> review_id

    async def create(self, review: FeatureReview) -> FeatureReview:
        if review.review_id in self._store:
            raise DuplicateEntityError("FeatureReview", review.review_id)
        self._store[review.review_id] = review.model_copy(deep=True)
        self._by_detection_id[review.detection_id] = review.review_id
        return review.model_copy(deep=True)

    async def get(self, review_id: str) -> FeatureReview | None:
        stored = self._store.get(review_id)
        return stored.model_copy(deep=True) if stored else None

    async def find_by_detection_id(self, detection_id: str) -> FeatureReview | None:
        review_id = self._by_detection_id.get(detection_id)
        if review_id is None:
            return None
        return await self.get(review_id)

    async def update_if_version(self, review_id: str, expected_version: int, updated_review: FeatureReview) -> FeatureReview:
        current = self._store.get(review_id)
        if current is None:
            raise EntityNotFoundError("FeatureReview", review_id)
        if current.version != expected_version:
            raise VersionConflictError(review_id, expected_version, current.version)
        new_review = updated_review.model_copy(update={"version": expected_version + 1})
        self._store[review_id] = new_review.model_copy(deep=True)
        return new_review.model_copy(deep=True)

    async def query(self, query: FeatureReviewQuery) -> list[FeatureReview]:
        results = []
        for review in self._store.values():
            if query.status is not None and review.status != query.status:
                continue
            if query.since is not None and review.created_at < query.since:
                continue
            if query.until is not None and review.created_at > query.until:
                continue
            results.append(review.model_copy(deep=True))
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results[: query.limit]


class InMemoryRemediationVerificationRepository:
    def __init__(self):
        self._store: dict[str, RemediationVerification] = {}
        self._by_resolution: dict[str, list[str]] = {}  # resolution_id -> [verification_id, ...]
        self._by_idempotency_key: dict[str, str] = {}  # idempotency_key -> verification_id

    async def create(self, verification: RemediationVerification) -> RemediationVerification:
        if verification.verification_id in self._store:
            raise DuplicateEntityError("RemediationVerification", verification.verification_id)
        self._store[verification.verification_id] = verification.model_copy(deep=True)
        self._by_resolution.setdefault(verification.resolution_id, []).append(verification.verification_id)
        self._by_idempotency_key[verification.idempotency_key] = verification.verification_id
        return verification.model_copy(deep=True)

    async def get(self, verification_id: str) -> RemediationVerification | None:
        stored = self._store.get(verification_id)
        return stored.model_copy(deep=True) if stored else None

    async def find_by_resolution(self, resolution_id: str) -> list[RemediationVerification]:
        ids = self._by_resolution.get(resolution_id, [])
        return [self._store[i].model_copy(deep=True) for i in ids]

    async def find_by_idempotency_key(self, idempotency_key: str) -> RemediationVerification | None:
        verification_id = self._by_idempotency_key.get(idempotency_key)
        if verification_id is None:
            return None
        return await self.get(verification_id)

    async def update_if_version(
        self, verification_id: str, expected_version: int, updated_verification: RemediationVerification
    ) -> RemediationVerification:
        current = self._store.get(verification_id)
        if current is None:
            raise EntityNotFoundError("RemediationVerification", verification_id)
        if current.version != expected_version:
            raise VersionConflictError(verification_id, expected_version, current.version)
        new_verification = updated_verification.model_copy(update={"version": expected_version + 1})
        self._store[verification_id] = new_verification.model_copy(deep=True)
        return new_verification.model_copy(deep=True)

    async def query(self, query: RemediationVerificationQuery) -> list[RemediationVerification]:
        results = []
        for verification in self._store.values():
            if query.outcome is not None and verification.outcome != query.outcome:
                continue
            if query.status is not None and verification.status != query.status:
                continue
            if query.since is not None and verification.verification_started_at < query.since:
                continue
            if query.until is not None and verification.verification_started_at > query.until:
                continue
            results.append(verification.model_copy(deep=True))
        results.sort(key=lambda v: v.verification_started_at, reverse=True)
        return results[: query.limit]
