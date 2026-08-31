"""Workflow-facing response schemas. Never the raw WorkflowState/Artifact/
AgentExecution/Decision domain models — see app/api/schemas/common.py.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.domain import AgentExecution, Artifact, Decision, WorkflowStage, WorkflowState, WorkflowStatus


class WorkflowRunResult(BaseModel):
    """The response for POST /workflows/{id}/run — see
    app/api/routes/workflows.py. Deliberately carries no raw LLM output;
    every field here is derived from durable state (WorkflowState,
    Artifact, Decision records) already produced by the existing
    OrchestrationService.run_to_completion()."""

    workflow_id: str
    initial_stage: WorkflowStage
    final_stage: WorkflowStage
    final_status: WorkflowStatus
    stages_executed: list[str]
    artifacts_created: int
    decisions_created: int
    retries_used: int
    duration_ms: float
    human_action_required: bool


class WorkflowSummary(BaseModel):
    workflow_id: str
    ticket_title: str
    status: WorkflowStatus
    current_stage: WorkflowStage
    artifact_count: int
    remediation_outcome: str | None = None

    @classmethod
    def from_domain(cls, workflow: WorkflowState) -> "WorkflowSummary":
        return cls(
            workflow_id=workflow.workflow_id,
            ticket_title=workflow.ticket.title,
            status=workflow.status,
            current_stage=workflow.current_stage,
            artifact_count=len(workflow.artifact_ids),
            remediation_outcome=workflow.metadata.get("remediation_outcome"),
        )


class WorkflowDetail(BaseModel):
    workflow_id: str
    ticket_title: str
    ticket_description: str
    status: WorkflowStatus
    current_stage: WorkflowStage
    artifact_ids: list[str]
    execution_ids: list[str]
    active_decision_id: str | None
    active_incident_ids: list[str]
    # A narrow, explicit allow-list of metadata keys safe to expose — never
    # the raw metadata dict, which can carry workspace_path (a local
    # filesystem path) and other internal bookkeeping never meant for a
    # client. See docs/architecture/control_plane_api.md "Response models".
    remediation_outcome: str | None
    remediation_strategy: str | None
    latest_verification_id: str | None
    source_detection_id: str | None
    review_id: str | None

    @classmethod
    def from_domain(cls, workflow: WorkflowState) -> "WorkflowDetail":
        metadata: dict[str, Any] = workflow.metadata
        return cls(
            workflow_id=workflow.workflow_id,
            ticket_title=workflow.ticket.title,
            ticket_description=workflow.ticket.description,
            status=workflow.status,
            current_stage=workflow.current_stage,
            artifact_ids=list(workflow.artifact_ids),
            execution_ids=list(workflow.execution_ids),
            active_decision_id=workflow.active_decision_id,
            active_incident_ids=list(workflow.active_incident_ids),
            remediation_outcome=metadata.get("remediation_outcome"),
            remediation_strategy=metadata.get("remediation_strategy"),
            latest_verification_id=metadata.get("latest_verification_id"),
            source_detection_id=metadata.get("source_detection_id"),
            review_id=metadata.get("review_id"),
        )


class ArtifactSummary(BaseModel):
    artifact_id: str
    artifact_type: str
    version: int
    created_by: str
    created_at: datetime
    status: WorkflowStatus
    # Additive, optional — set only when the producing agent ran in demo
    # mode (Settings.codegen_demo_mode/testing_demo_mode). Read from
    # Artifact.payload, the existing arbitrary-data extension point —
    # Artifact itself has no dedicated metadata field, so no domain model
    # change was made to support this. None for every real-mode artifact,
    # unchanged from before demo mode existed.
    execution_mode: str | None = None

    @classmethod
    def from_domain(cls, artifact: Artifact) -> "ArtifactSummary":
        return cls(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type.value,
            version=artifact.version,
            created_by=artifact.created_by,
            created_at=artifact.created_at,
            status=artifact.status,
            execution_mode=artifact.payload.get("execution_mode"),
        )


class ExecutionSummary(BaseModel):
    execution_id: str
    agent_name: str
    status: WorkflowStatus
    started_at: datetime
    completed_at: datetime | None
    retry_count: int
    error_code: str | None
    error_message: str | None

    @classmethod
    def from_domain(cls, execution: AgentExecution) -> "ExecutionSummary":
        return cls(
            execution_id=execution.execution_id,
            agent_name=execution.agent_name,
            status=execution.status,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            retry_count=execution.retry_count,
            error_code=execution.error.code if execution.error else None,
            error_message=execution.error.message if execution.error else None,
        )


class DecisionSummary(BaseModel):
    decision_id: str
    action: str
    target_agent: str | None
    reason: str
    confidence: float
    source: str
    created_at: datetime

    @classmethod
    def from_domain(cls, decision: Decision) -> "DecisionSummary":
        return cls(
            decision_id=decision.decision_id,
            action=decision.action.value,
            target_agent=decision.target_agent,
            reason=decision.reason,
            confidence=decision.confidence,
            source=decision.source.value,
            created_at=decision.created_at,
        )
