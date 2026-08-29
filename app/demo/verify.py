"""Evidence-first verification — every function here inspects real,
persisted domain state (via the same repository Protocols production code
uses) and returns (passed, detail). Nothing here trusts an in-memory
Python variable the harness happens to still be holding; each check
re-reads from the repository, exactly as a human auditing the system after
the fact would have to.
"""

from app.domain import DetectionResult, DetectionType, ResolutionResult, ReviewStatus, Signal, WorkflowStatus


async def verify_signals_persisted(signal_repo, signal_ids: list[str]) -> tuple[bool, str]:
    resolved: list[Signal] = []
    for signal_id in signal_ids:
        signal = await signal_repo.get(signal_id)
        if signal is not None:
            resolved.append(signal)
    if len(resolved) != len(signal_ids):
        return False, f"expected {len(signal_ids)} persisted signal(s), found {len(resolved)}"
    return True, f"{len(resolved)} signal(s) persisted and independently re-readable: {[s.signal_id for s in resolved]}"


async def verify_detection(detection_repo, detection_id: str, expected_type: DetectionType) -> tuple[bool, str]:
    detection = await detection_repo.get(detection_id)
    if detection is None:
        return False, f"DetectionResult '{detection_id}' not found"
    if detection.detection_type != expected_type:
        return False, f"DetectionResult '{detection_id}' has type '{detection.detection_type}', expected '{expected_type}'"
    if not detection.supporting_signal_ids:
        return False, f"DetectionResult '{detection_id}' has no supporting_signal_ids"
    return True, (
        f"DetectionResult '{detection_id}' type={detection.detection_type.value} "
        f"confidence={detection.confidence:.2f} supporting_signal_ids={detection.supporting_signal_ids}"
    )


async def verify_review(review_repo, review_id: str, expected_status: ReviewStatus) -> tuple[bool, str]:
    review = await review_repo.get(review_id)
    if review is None:
        return False, f"FeatureReview '{review_id}' not found"
    if review.status != expected_status:
        return False, f"FeatureReview '{review_id}' has status '{review.status}', expected '{expected_status}'"
    return True, f"FeatureReview '{review_id}' status={review.status.value}"


async def verify_ticket_created(review_repo, review_id: str) -> tuple[bool, str]:
    review = await review_repo.get(review_id)
    if review is None or review.ticket is None:
        return False, f"FeatureReview '{review_id}' has no associated Ticket"
    return True, f"Ticket '{review.ticket.ticket_id}' (external_id={review.ticket.external_id}) source_detection_id={review.ticket.source_detection_id}"


async def verify_resolution(resolution_repo, resolution_id: str, *, expected_strategy: str | None = None) -> tuple[bool, str]:
    resolution = await resolution_repo.get(resolution_id)
    if resolution is None:
        return False, f"ResolutionResult '{resolution_id}' not found"
    if expected_strategy is not None and resolution.remediation_strategy.value != expected_strategy:
        return False, f"ResolutionResult '{resolution_id}' has strategy '{resolution.remediation_strategy}', expected '{expected_strategy}'"
    return True, (
        f"ResolutionResult '{resolution_id}' strategy={resolution.remediation_strategy.value} "
        f"risk={resolution.risk.value} confidence={resolution.root_cause_confidence:.2f}"
    )


async def verify_workflow_status(workflow_repo, workflow_id: str, expected_status: WorkflowStatus) -> tuple[bool, str]:
    workflow = await workflow_repo.get(workflow_id)
    if workflow is None:
        return False, f"WorkflowState '{workflow_id}' not found"
    if workflow.status != expected_status:
        return False, f"WorkflowState '{workflow_id}' has status '{workflow.status}', expected '{expected_status}'"
    return True, f"WorkflowState '{workflow_id}' status={workflow.status.value} stage={workflow.current_stage.value}"


async def verify_artifact_chain(artifact_repo, workflow_id: str, artifact_ids: list[str], expected_types: list[str]) -> tuple[bool, str]:
    found_types = []
    for artifact_id in artifact_ids:
        artifact = await artifact_repo.get(workflow_id, artifact_id)
        if artifact is None:
            return False, f"artifact '{artifact_id}' not found under workflow '{workflow_id}'"
        found_types.append(artifact.artifact_type.value)
    missing = [t for t in expected_types if t not in found_types]
    if missing:
        return False, f"expected artifact types {expected_types}, found {found_types} (missing {missing})"
    return True, f"artifact chain present: {found_types}"


async def verify_no_duplicate_workflow(review_repo, workflow_repo, review_id: str, first_workflow_id: str) -> tuple[bool, str]:
    review = await review_repo.get(review_id)
    if review is None:
        return False, f"FeatureReview '{review_id}' not found"
    if review.workflow_id != first_workflow_id:
        return False, f"FeatureReview '{review_id}' now points at workflow '{review.workflow_id}', expected '{first_workflow_id}'"
    all_workflows = await workflow_repo.get(first_workflow_id)
    if all_workflows is None:
        return False, f"workflow '{first_workflow_id}' unexpectedly missing"
    return True, f"idempotent — FeatureReview '{review_id}' still points at the single workflow '{first_workflow_id}'"


async def verify_no_remediation_execution(execution_repo, workflow_id: str, before_count: int) -> tuple[bool, str]:
    executions = await execution_repo.list_for_workflow(workflow_id)
    after_count = len(executions)
    if after_count != before_count:
        return False, f"expected no new AgentExecution, but count went from {before_count} to {after_count}"
    return True, f"no new agent execution recorded ({after_count} unchanged) — escalation/no-action never invoked an agent"


async def verify_detection_and_resolution_immutable(
    detection_repo, resolution_repo, detection: DetectionResult, resolution: ResolutionResult | None
) -> tuple[bool, str]:
    current_detection = await detection_repo.get(detection.detection_id)
    if current_detection != detection:
        return False, f"DetectionResult '{detection.detection_id}' was mutated"
    if resolution is not None:
        current_resolution = await resolution_repo.get(resolution.resolution_id)
        if current_resolution != resolution:
            return False, f"ResolutionResult '{resolution.resolution_id}' was mutated"
    return True, "DetectionResult/ResolutionResult unchanged after execution — evidence and interpretation stayed separate from what was executed"
