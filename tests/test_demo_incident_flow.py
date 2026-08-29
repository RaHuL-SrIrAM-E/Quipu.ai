"""Tests for the Scenario 2 (Production Incident -> Remediation) demo
harness. Runs the real DemoHarness — no live Gemini/Cloud
Monitoring/Logging/Run/Firestore required."""

import pytest

from app.demo import DemoHarness
from app.domain import WorkflowStatus


@pytest.mark.asyncio
async def test_incident_flow_verification_passes():
    summary = await DemoHarness().run_incident_flow()
    assert summary.verification_status == "passed"
    assert all(step.passed for step in summary.steps)


@pytest.mark.asyncio
async def test_incident_flow_reaches_completed_status():
    summary = await DemoHarness().run_incident_flow()
    assert summary.final_status == WorkflowStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_incident_flow_remediation_outcome_never_claims_resolved():
    summary = await DemoHarness().run_incident_flow()
    assert summary.remediation_outcome == "deployed_pending_verification"
    assert summary.remediation_outcome != "incident_resolved"


@pytest.mark.asyncio
async def test_incident_flow_escalation_occurred_for_unsafe_case():
    summary = await DemoHarness().run_incident_flow()
    assert summary.escalated is True


@pytest.mark.asyncio
async def test_incident_flow_provenance_chain_survives():
    harness = DemoHarness()
    summary = await harness.run_incident_flow()

    detection = await harness.detection_repo.get(summary.detection_id)
    assert set(detection.supporting_signal_ids) == set(summary.signal_ids)

    resolution = await harness.resolution_repo.get(summary.resolution_id)
    assert resolution.detection_id == summary.detection_id
    assert resolution.workflow_id == summary.workflow_id

    workflow = await harness.workflow_repo.get(summary.workflow_id)
    # remediation_resolution_ids accumulates every resolution ever actioned
    # on this workflow (including the later unsafe/escalated one) — the
    # idempotency list, not a single "current" marker.
    assert summary.resolution_id in workflow.metadata["remediation_resolution_ids"]


@pytest.mark.asyncio
async def test_incident_flow_target_agent_spoofing_ignored():
    """The fake IncidentResolutionAgent proposal claims
    target_agent='deployment_agent' for a code_fix strategy. Two
    independent layers reject that claim: IncidentResolutionAgent itself
    already derives target_agent deterministically from the strategy
    before ever persisting it (Level 3.3 — the model's raw claim never
    survives into ResolutionResult at all), and
    start_remediation_from_resolution independently re-derives the entry
    stage from the strategy too, never reading target_agent (Level 3.6)."""
    harness = DemoHarness()
    summary = await harness.run_incident_flow()
    step = next(s for s in summary.steps if s.name == "remediation_authorized_ignoring_spoofed_target")
    assert step.passed
    resolution = await harness.resolution_repo.get(summary.resolution_id)
    assert resolution.target_agent == "codegen_agent"  # already corrected at the agent layer, not "deployment_agent"
    assert "codegen" in step.detail  # and execution actually went to codegen


@pytest.mark.asyncio
async def test_incident_flow_testing_failure_blocked_deployment():
    """Failure path #3 — a genuinely broken on-disk test blocks Deployment
    and routes back to Codegen via the existing deterministic machinery."""
    summary = await DemoHarness().run_incident_flow()
    step = next(s for s in summary.steps if s.name == "testing_failure_blocks_deployment_and_retries")
    assert step.passed


@pytest.mark.asyncio
async def test_incident_flow_unsafe_remediation_never_executes_an_agent():
    """Failure path #4 — high-risk resolution must escalate, not execute."""
    harness = DemoHarness()
    summary = await harness.run_incident_flow()
    escalate_step = next(s for s in summary.steps if s.name == "unsafe_remediation_escalated_not_executed")
    no_exec_step = next(s for s in summary.steps if s.name == "no_agent_execution_for_escalation")
    assert escalate_step.passed
    assert no_exec_step.passed


@pytest.mark.asyncio
async def test_incident_flow_idempotent_remediation_rerun():
    """Failure path #5 (incident variant) — re-submitting the same
    resolution_id never creates a second remediation workflow."""
    summary = await DemoHarness().run_incident_flow()
    step = next(s for s in summary.steps if s.name == "idempotent_remediation_rerun")
    assert step.passed


@pytest.mark.asyncio
async def test_incident_flow_detection_and_resolution_remain_immutable():
    harness = DemoHarness()
    summary = await harness.run_incident_flow()
    detection_before = await harness.detection_repo.get(summary.detection_id)
    resolution_before = await harness.resolution_repo.get(summary.resolution_id)
    # Re-fetch again after the full scenario (including the unsafe branch
    # and idempotent rerun) completed — still byte-identical.
    detection_after = await harness.detection_repo.get(summary.detection_id)
    resolution_after = await harness.resolution_repo.get(summary.resolution_id)
    assert detection_before == detection_after
    assert resolution_before == resolution_after


@pytest.mark.asyncio
async def test_incident_flow_adk_loop_agent_constructed():
    """Visibly exercises the real ADK LoopAgent construction path."""
    summary = await DemoHarness().run_incident_flow()
    assert summary.extra["adk_loop_agent_sub_agents"] == ["codegen_agent", "testing_agent", "loop_evaluator"]


@pytest.mark.asyncio
async def test_incident_flow_no_capability_bypass():
    from app.agent_runtime.capabilities import AgentCapability
    from app.agents.incident_resolution import IncidentResolutionAgent

    assert AgentCapability.RESOLVE_INCIDENT not in IncidentResolutionAgent().capabilities
    assert AgentCapability.DEPLOY not in IncidentResolutionAgent().capabilities
    assert AgentCapability.WRITE_CODE not in IncidentResolutionAgent().capabilities


@pytest.mark.asyncio
async def test_incident_flow_demo_summary_is_json_serializable():
    import json

    summary = await DemoHarness().run_incident_flow()
    payload = json.dumps(summary.model_dump(mode="json"))
    assert "incident" in payload
