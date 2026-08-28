"""In-memory repository implementations — deterministic, no external
dependency. Used for unit tests and local development before Firestore is
configured. InMemoryWorkflowRepository enforces the same version-conflict
semantics as FirestoreWorkflowRepository, so tests exercise real concurrency
behaviour without needing a live Firestore connection.
"""

from app.domain import AgentExecution, Artifact, Decision, WorkflowState
from app.persistence.errors import DuplicateEntityError, EntityNotFoundError, VersionConflictError
from app.persistence.repositories.incident import IncidentRecord


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
