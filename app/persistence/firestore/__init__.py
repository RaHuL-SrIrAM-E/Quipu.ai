"""Firestore-backed repository implementations. NOT imported by
app.persistence's own __init__.py — the google-cloud-firestore SDK stays
isolated to this subpackage; import it explicitly:

    from app.persistence.firestore import FirestoreWorkflowRepository
"""

from app.persistence.firestore.client import FirestoreConfigError, get_firestore_client
from app.persistence.firestore.repositories import (
    FirestoreAgentExecutionRepository,
    FirestoreArtifactRepository,
    FirestoreDecisionRepository,
    FirestoreIncidentRepository,
    FirestoreSignalRepository,
    FirestoreWorkflowRepository,
)

__all__ = [
    "FirestoreAgentExecutionRepository",
    "FirestoreArtifactRepository",
    "FirestoreConfigError",
    "FirestoreDecisionRepository",
    "FirestoreIncidentRepository",
    "FirestoreSignalRepository",
    "FirestoreWorkflowRepository",
    "get_firestore_client",
]
