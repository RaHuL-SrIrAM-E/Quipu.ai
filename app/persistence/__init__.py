"""Quipu's persistence layer: durable workflow/execution/artifact/decision
state. This is Quipu's own operational state — NOT enterprise knowledge (that
stays in app.knowledge, backed by Cloud Storage / Agent Search). See
docs/architecture/persistence.md.

Depends on app.domain only. google-cloud-firestore is isolated to
app.persistence.firestore, which this __init__ deliberately does not import —
`import app.persistence` never pulls in the Firestore SDK. Callers who want
Firestore import it explicitly: `from app.persistence.firestore import
FirestoreWorkflowRepository`.
"""

from app.persistence.errors import DuplicateEntityError, EntityNotFoundError, PersistenceError, VersionConflictError
from app.persistence.repositories import (
    AgentExecutionRepository,
    ArtifactRepository,
    DecisionRepository,
    IncidentRecord,
    IncidentRepository,
    WorkflowRepository,
)

__all__ = [
    "AgentExecutionRepository",
    "ArtifactRepository",
    "DecisionRepository",
    "DuplicateEntityError",
    "EntityNotFoundError",
    "IncidentRecord",
    "IncidentRepository",
    "PersistenceError",
    "VersionConflictError",
    "WorkflowRepository",
]
