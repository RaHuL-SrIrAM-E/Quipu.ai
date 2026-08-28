"""AgentStatus — an agent's own execution lifecycle, distinct from WorkflowStatus."""

from enum import StrEnum


class AgentStatus(StrEnum):
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
