"""IncidentRepository — minimal, provisional.

app.domain has no Incident model yet (WorkflowState only carries
active_incident_ids: list[str]). Per Level 1.4 scope, this task does NOT
invent a full Incident domain model — that belongs with the Incident
Resolution stage. IncidentRecord below is a deliberately small,
persistence-local stand-in (not part of app.domain) with just enough shape
to be stored and queried; expect it to be replaced by a proper
app.domain.incident.Incident once that stage defines what an incident
actually needs to carry.
"""

import uuid
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.domain.enums import WorkflowStatus


class IncidentRecord(BaseModel):
    """Provisional — see module docstring."""

    incident_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    summary: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class IncidentRepository(Protocol):
    async def save(self, incident: IncidentRecord) -> IncidentRecord: ...

    async def get(self, workflow_id: str, incident_id: str) -> IncidentRecord | None: ...

    async def list_for_workflow(self, workflow_id: str) -> list[IncidentRecord]: ...
