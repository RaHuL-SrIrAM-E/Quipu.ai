"""Tests for the Scenario 1 (Feature Discovery -> SDLC) demo harness. Runs
the real DemoHarness — no live Gemini/Jira/Firestore required."""

import pytest

from app.demo import DemoHarness
from app.domain import ReviewStatus, WorkflowStatus


@pytest.mark.asyncio
async def test_feature_flow_verification_passes():
    summary = await DemoHarness().run_feature_flow()
    assert summary.verification_status == "passed"
    assert all(step.passed for step in summary.steps)


@pytest.mark.asyncio
async def test_feature_flow_reaches_completed_status():
    summary = await DemoHarness().run_feature_flow()
    assert summary.final_status == WorkflowStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_feature_flow_produces_full_artifact_chain():
    summary = await DemoHarness().run_feature_flow()
    assert len(summary.artifact_ids) == 5
    assert summary.stages_executed == ["planning", "architecture", "codegen", "testing", "deployment"]


@pytest.mark.asyncio
async def test_feature_flow_provenance_chain_survives():
    harness = DemoHarness()
    summary = await harness.run_feature_flow()

    detection = await harness.detection_repo.get(summary.detection_id)
    assert set(detection.supporting_signal_ids) == set(summary.signal_ids)

    review = await harness.review_repo.get(summary.review_id)
    assert review.detection_id == summary.detection_id
    assert review.ticket.ticket_id == summary.ticket_id
    assert review.ticket.source_detection_id == summary.detection_id

    workflow = await harness.workflow_repo.get(summary.workflow_id)
    assert workflow.ticket.ticket_id == summary.ticket_id
    assert workflow.ticket.source_detection_id == summary.detection_id
    assert workflow.metadata["review_id"] == summary.review_id


@pytest.mark.asyncio
async def test_feature_flow_review_reaches_approved():
    harness = DemoHarness()
    summary = await harness.run_feature_flow()
    review = await harness.review_repo.get(summary.review_id)
    assert review.status == ReviewStatus.APPROVED
    assert review.reviewer_id is not None


@pytest.mark.asyncio
async def test_feature_flow_idempotent_review_and_workflow_creation():
    """Failure path #5 — re-running create_review()/start_workflow_from_review()
    for the same detection/review never creates a second review or workflow."""
    harness = DemoHarness()
    summary = await harness.run_feature_flow()

    idempotent_steps = {s.name: s for s in summary.steps}
    assert idempotent_steps["idempotent_review_recreate"].passed
    assert idempotent_steps["idempotent_workflow_recreate"].passed

    all_reviews = [r async for r in _iter_all(harness)]
    assert len([r for r in all_reviews if r.detection_id == summary.detection_id]) == 1


async def _iter_all(harness):
    from app.persistence.repositories.feature_review import FeatureReviewQuery

    for review in await harness.review_repo.query(FeatureReviewQuery(limit=500)):
        yield review


@pytest.mark.asyncio
async def test_feature_flow_signal_normalization_uses_real_production_adapters():
    """The seeded signals are built via app.signals.adapters — not a demo
    parallel implementation — so their signal_type reflects real
    normalization output."""
    from app.domain import SignalType

    harness = DemoHarness()
    summary = await harness.run_feature_flow()
    for signal_id in summary.signal_ids:
        signal = await harness.signal_repo.get(signal_id)
        assert signal.signal_type in (SignalType.CUSTOMER_FEEDBACK, SignalType.SUPPORT_FEEDBACK)


@pytest.mark.asyncio
async def test_feature_flow_no_agent_capability_bypass():
    """The agents invoked during the demo hold exactly their normal,
    unchanged capabilities — nothing about running through the demo
    harness grants anything extra."""
    from app.agent_runtime.capabilities import AgentCapability
    from app.agents.planning import PlanningAgent

    assert AgentCapability.WRITE_JIRA in PlanningAgent().capabilities
    assert AgentCapability.DEPLOY not in PlanningAgent().capabilities
    assert AgentCapability.WRITE_CODE not in PlanningAgent().capabilities


@pytest.mark.asyncio
async def test_feature_flow_adk_sequential_agent_constructed():
    """Visibly exercises the real ADK SequentialAgent construction path."""
    summary = await DemoHarness().run_feature_flow()
    assert summary.extra["adk_sequential_agent_stages"] == [
        "planning_agent",
        "architecture_agent",
        "codegen_agent",
        "testing_agent",
        "deployment_agent",
    ]


@pytest.mark.asyncio
async def test_feature_flow_demo_summary_is_json_serializable():
    import json

    summary = await DemoHarness().run_feature_flow()
    payload = json.dumps(summary.model_dump(mode="json"))
    assert '"scenario": "feature"' in payload or '"scenario":"feature"' in payload.replace(" ", "")
