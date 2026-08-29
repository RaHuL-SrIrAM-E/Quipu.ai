"""Firestore repository implementations.

Collection layout (workflow-centric — see docs/architecture/persistence.md):

    workflows/{workflow_id}
    workflows/{workflow_id}/executions/{execution_id}
    workflows/{workflow_id}/artifacts/{artifact_id}
    workflows/{workflow_id}/decisions/{decision_id}
    workflows/{workflow_id}/incidents/{incident_id}

Signal is the one exception (Level 3, see
docs/architecture/signal_platform.md "Persistence"): most signals exist
before any workflow does, and Detecting will need to query across all of
them, not within one workflow's subcollection — so Signal gets its own
top-level collection instead:

    signals/{signal_id}

This is the ONLY place (besides client.py/errors.py in this same package)
allowed to import google.cloud.firestore. Nothing in app.domain,
app.agent_runtime, or app.knowledge depends on it, and app.persistence's own
__init__.py does not import this module either — callers who want Firestore
import it explicitly, exactly like the Google Search knowledge backend.
"""

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.domain import AgentExecution, Artifact, Decision, Signal, WorkflowState
from app.persistence.errors import DuplicateEntityError, EntityNotFoundError, VersionConflictError
from app.persistence.firestore.errors import translate_firestore_error
from app.persistence.repositories.incident import IncidentRecord
from app.persistence.repositories.signal import SignalQuery
from app.persistence.serialization import from_firestore_dict, to_firestore_dict

_WORKFLOWS = "workflows"
_SIGNALS = "signals"


async def _update_workflow_txn(
    transaction: "firestore.AsyncTransaction",
    doc_ref: "firestore.AsyncDocumentReference",
    expected_version: int,
    data: dict,
) -> int:
    """The version-check-and-write logic, using only AsyncTransaction's public
    get()/set() methods. Kept as a standalone function (not a method) and
    separate from the @firestore.async_transactional wrapping in
    FirestoreWorkflowRepository.update_if_version so it's directly
    unit-testable against a fake transaction — see
    tests/test_firestore_persistence.py — without needing the real SDK's
    begin/commit/retry machinery, which is private API surface not meant to
    be mocked.
    """
    snapshot = None
    async for candidate in transaction.get(doc_ref):
        snapshot = candidate

    if snapshot is None or not snapshot.exists:
        raise EntityNotFoundError("WorkflowState", doc_ref.id)

    current = snapshot.to_dict() or {}
    actual_version = current.get("version")
    if actual_version != expected_version:
        raise VersionConflictError(doc_ref.id, expected_version, actual_version)

    data = dict(data)
    data["version"] = expected_version + 1
    transaction.set(doc_ref, data)
    return data["version"]


class FirestoreWorkflowRepository:
    def __init__(self, client: "firestore.AsyncClient"):
        self._client = client

    def _doc(self, workflow_id: str):
        return self._client.collection(_WORKFLOWS).document(workflow_id)

    async def create(self, workflow: WorkflowState) -> WorkflowState:
        doc_ref = self._doc(workflow.workflow_id)
        try:
            snapshot = await doc_ref.get()
            if snapshot.exists:
                raise DuplicateEntityError("WorkflowState", workflow.workflow_id)
            await doc_ref.set(to_firestore_dict(workflow))
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "WorkflowState", workflow.workflow_id) from exc
        return workflow

    async def get(self, workflow_id: str) -> WorkflowState | None:
        try:
            snapshot = await self._doc(workflow_id).get()
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "WorkflowState", workflow_id) from exc
        if not snapshot.exists:
            return None
        return from_firestore_dict(WorkflowState, snapshot.to_dict())

    async def update(self, workflow: WorkflowState) -> WorkflowState:
        doc_ref = self._doc(workflow.workflow_id)
        try:
            snapshot = await doc_ref.get()
            if not snapshot.exists:
                raise EntityNotFoundError("WorkflowState", workflow.workflow_id)
            await doc_ref.set(to_firestore_dict(workflow))
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "WorkflowState", workflow.workflow_id) from exc
        return workflow

    async def delete(self, workflow_id: str) -> None:
        doc_ref = self._doc(workflow_id)
        try:
            snapshot = await doc_ref.get()
            if not snapshot.exists:
                raise EntityNotFoundError("WorkflowState", workflow_id)
            await doc_ref.delete()
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "WorkflowState", workflow_id) from exc

    async def update_if_version(
        self, workflow_id: str, expected_version: int, updated_workflow: WorkflowState
    ) -> WorkflowState:
        doc_ref = self._doc(workflow_id)
        data = to_firestore_dict(updated_workflow)
        transaction = self._client.transaction()
        wrapped = firestore.async_transactional(_update_workflow_txn)
        try:
            new_version = await wrapped(transaction, doc_ref, expected_version, data)
        except (EntityNotFoundError, VersionConflictError):
            raise
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "WorkflowState", workflow_id) from exc
        return updated_workflow.model_copy(update={"version": new_version})


def _subcollection(client: "firestore.AsyncClient", workflow_id: str, name: str):
    return client.collection(_WORKFLOWS).document(workflow_id).collection(name)


class FirestoreArtifactRepository:
    def __init__(self, client: "firestore.AsyncClient"):
        self._client = client

    async def save(self, workflow_id: str, artifact: Artifact) -> Artifact:
        try:
            await _subcollection(self._client, workflow_id, "artifacts").document(artifact.artifact_id).set(
                to_firestore_dict(artifact)
            )
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Artifact", artifact.artifact_id) from exc
        return artifact

    async def get(self, workflow_id: str, artifact_id: str) -> Artifact | None:
        try:
            snapshot = await _subcollection(self._client, workflow_id, "artifacts").document(artifact_id).get()
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Artifact", artifact_id) from exc
        if not snapshot.exists:
            return None
        return from_firestore_dict(Artifact, snapshot.to_dict())

    async def list_for_workflow(self, workflow_id: str) -> list[Artifact]:
        try:
            results = []
            async for snapshot in _subcollection(self._client, workflow_id, "artifacts").stream():
                results.append(from_firestore_dict(Artifact, snapshot.to_dict()))
            return results
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Artifact", workflow_id) from exc


class FirestoreAgentExecutionRepository:
    def __init__(self, client: "firestore.AsyncClient"):
        self._client = client

    async def create(self, execution: AgentExecution) -> AgentExecution:
        doc_ref = _subcollection(self._client, execution.workflow_id, "executions").document(execution.execution_id)
        try:
            snapshot = await doc_ref.get()
            if snapshot.exists:
                raise DuplicateEntityError("AgentExecution", execution.execution_id)
            await doc_ref.set(to_firestore_dict(execution))
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "AgentExecution", execution.execution_id) from exc
        return execution

    async def get(self, workflow_id: str, execution_id: str) -> AgentExecution | None:
        try:
            snapshot = await _subcollection(self._client, workflow_id, "executions").document(execution_id).get()
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "AgentExecution", execution_id) from exc
        if not snapshot.exists:
            return None
        return from_firestore_dict(AgentExecution, snapshot.to_dict())

    async def list_for_workflow(self, workflow_id: str) -> list[AgentExecution]:
        try:
            results = []
            async for snapshot in _subcollection(self._client, workflow_id, "executions").stream():
                results.append(from_firestore_dict(AgentExecution, snapshot.to_dict()))
            return results
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "AgentExecution", workflow_id) from exc

    async def update(self, execution: AgentExecution) -> AgentExecution:
        doc_ref = _subcollection(self._client, execution.workflow_id, "executions").document(execution.execution_id)
        try:
            snapshot = await doc_ref.get()
            if not snapshot.exists:
                raise EntityNotFoundError("AgentExecution", execution.execution_id)
            await doc_ref.set(to_firestore_dict(execution))
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "AgentExecution", execution.execution_id) from exc
        return execution


class FirestoreDecisionRepository:
    def __init__(self, client: "firestore.AsyncClient"):
        self._client = client

    async def save(self, workflow_id: str, decision: Decision) -> Decision:
        try:
            await _subcollection(self._client, workflow_id, "decisions").document(decision.decision_id).set(
                to_firestore_dict(decision)
            )
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Decision", decision.decision_id) from exc
        return decision

    async def get(self, workflow_id: str, decision_id: str) -> Decision | None:
        try:
            snapshot = await _subcollection(self._client, workflow_id, "decisions").document(decision_id).get()
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Decision", decision_id) from exc
        if not snapshot.exists:
            return None
        return from_firestore_dict(Decision, snapshot.to_dict())

    async def list_for_workflow(self, workflow_id: str) -> list[Decision]:
        try:
            results = []
            async for snapshot in _subcollection(self._client, workflow_id, "decisions").stream():
                results.append(from_firestore_dict(Decision, snapshot.to_dict()))
            return results
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Decision", workflow_id) from exc


class FirestoreIncidentRepository:
    def __init__(self, client: "firestore.AsyncClient"):
        self._client = client

    async def save(self, incident: IncidentRecord) -> IncidentRecord:
        try:
            await _subcollection(self._client, incident.workflow_id, "incidents").document(incident.incident_id).set(
                to_firestore_dict(incident)
            )
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "IncidentRecord", incident.incident_id) from exc
        return incident

    async def get(self, workflow_id: str, incident_id: str) -> IncidentRecord | None:
        try:
            snapshot = await _subcollection(self._client, workflow_id, "incidents").document(incident_id).get()
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "IncidentRecord", incident_id) from exc
        if not snapshot.exists:
            return None
        return from_firestore_dict(IncidentRecord, snapshot.to_dict())

    async def list_for_workflow(self, workflow_id: str) -> list[IncidentRecord]:
        try:
            results = []
            async for snapshot in _subcollection(self._client, workflow_id, "incidents").stream():
                results.append(from_firestore_dict(IncidentRecord, snapshot.to_dict()))
            return results
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "IncidentRecord", workflow_id) from exc


class FirestoreSignalRepository:
    """Top-level `signals/{signal_id}` collection — not workflow-scoped, see
    module docstring. `query()` chains equality `.where()` filters plus an
    optional observed_at range; combining a range filter with multiple
    equality filters may require a composite Firestore index in production
    (Firestore returns a `FailedPrecondition` with a console link to create
    one the first time an uncovered combination runs — not pre-created
    here, since the actual combinations Monitoring/Detecting will need
    aren't known yet at this level)."""

    def __init__(self, client: "firestore.AsyncClient"):
        self._client = client

    def _collection(self):
        return self._client.collection(_SIGNALS)

    async def save(self, signal: Signal) -> Signal:
        try:
            await self._collection().document(signal.signal_id).set(to_firestore_dict(signal))
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Signal", signal.signal_id) from exc
        return signal

    async def get(self, signal_id: str) -> Signal | None:
        try:
            snapshot = await self._collection().document(signal_id).get()
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Signal", signal_id) from exc
        if not snapshot.exists:
            return None
        return from_firestore_dict(Signal, snapshot.to_dict())

    async def find_by_fingerprint(self, fingerprint: str) -> Signal | None:
        try:
            query = self._collection().where(filter=FieldFilter("fingerprint", "==", fingerprint)).limit(1)
            async for snapshot in query.stream():
                return from_firestore_dict(Signal, snapshot.to_dict())
            return None
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Signal", fingerprint) from exc

    async def query(self, query: SignalQuery) -> list[Signal]:
        firestore_query = self._collection()
        if query.signal_type is not None:
            firestore_query = firestore_query.where(filter=FieldFilter("signal_type", "==", query.signal_type.value))
        if query.source is not None:
            firestore_query = firestore_query.where(filter=FieldFilter("source", "==", query.source.value))
        if query.service_name is not None:
            firestore_query = firestore_query.where(filter=FieldFilter("service_name", "==", query.service_name))
        if query.environment is not None:
            firestore_query = firestore_query.where(filter=FieldFilter("environment", "==", query.environment))
        if query.severity is not None:
            firestore_query = firestore_query.where(filter=FieldFilter("severity", "==", query.severity.value))
        if query.since is not None:
            firestore_query = firestore_query.where(filter=FieldFilter("observed_at", ">=", query.since))
        if query.until is not None:
            firestore_query = firestore_query.where(filter=FieldFilter("observed_at", "<=", query.until))
        firestore_query = firestore_query.order_by("observed_at", direction=firestore.Query.DESCENDING).limit(query.limit)

        try:
            results = []
            async for snapshot in firestore_query.stream():
                results.append(from_firestore_dict(Signal, snapshot.to_dict()))
            return results
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_firestore_error(exc, "Signal", "query") from exc
