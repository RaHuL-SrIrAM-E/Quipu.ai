"""SignalRepository — persistence for Signal, independent of WorkflowState.

Unlike Artifact/AgentExecution/Decision/Incident, Signals are NOT
workflow-scoped: most signals (a metric anomaly, a piece of customer
feedback) exist before any workflow does, and the future Detecting Agent
needs to query across all of them to look for correlation, not within one
workflow's artifact_ids. So SignalRepository has no workflow_id parameter
anywhere — see docs/architecture/signal_platform.md "Persistence" for the
Firestore collection-layout consequence of this (top-level `signals/`, not
`workflows/{id}/signals/`).
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.domain import Signal, SignalSeverity, SignalSource, SignalType


class SignalQuery(BaseModel):
    """Filter dimensions Monitoring/Detecting are expected to need — time
    range, type, source, service, environment, severity. Deliberately not a
    general search API; all fields are optional equality/range filters,
    ANDed together. `limit` bounds the result size (repositories must not
    return unbounded result sets)."""

    signal_type: SignalType | None = None
    source: SignalSource | None = None
    service_name: str | None = None
    environment: str | None = None
    severity: SignalSeverity | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, gt=0, le=500)


@runtime_checkable
class SignalRepository(Protocol):
    async def save(self, signal: Signal) -> Signal:
        """Create-or-replace by signal_id — Signals are immutable evidence
        once persisted (see Signal's own docstring), so this is a simple
        upsert, same pattern as ArtifactRepository.save, not create-vs-update."""
        ...

    async def get(self, signal_id: str) -> Signal | None: ...

    async def find_by_fingerprint(self, fingerprint: str) -> Signal | None:
        """The deduplication boundary (Level 3 §14): a future ingestion
        pipeline calls this before save() to decide whether an equivalent
        signal already exists. This repository does not enforce dedup
        itself — see app.domain.signal.compute_fingerprint."""
        ...

    async def query(self, query: SignalQuery) -> list[Signal]: ...
