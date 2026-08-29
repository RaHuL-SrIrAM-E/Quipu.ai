"""Demo scenario seeding — POST /demo/scenarios/{scenario}. Runs one of
the two existing, deterministic DemoHarness scenarios directly against
the RUNNING API's own repositories (via DemoHarness's now-injectable
constructor — see app/demo/harness.py), so the seeded Signals/Detections/
Reviews/Resolutions/Verifications are immediately visible through every
normal query endpoint (GET /signals, /detections, /feature-reviews,
/resolutions, /verifications) and in the UI.

This router is registered by app/api/app.py ONLY when
Settings.demo_endpoints_enabled is true — when false, these paths simply
don't exist (a plain 404), not merely "disabled" behind an internal
check. Never enable this in a real production deployment.

No business logic lives here: DemoHarness.run_feature_flow()/
run_incident_flow() are the exact same, unmodified scenarios
tests/test_demo_feature_flow.py and tests/test_demo_incident_flow.py
already exercise — this route only wires them to the live container's
repositories and shapes the response.
"""

import time
from enum import Enum

from fastapi import APIRouter, Depends

from app.api.container import ApiContainer
from app.api.dependencies import get_container
from app.api.schemas.demo import DemoScenarioResult
from app.core.observability import get_logger
from app.demo.harness import DemoHarness

logger = get_logger("quipu.api.demo")
router = APIRouter(prefix="/demo", tags=["demo"])


class DemoScenario(str, Enum):
    """A closed, deterministic allow-list — FastAPI rejects any other
    path value with 422 before this handler ever runs. There is no way
    to request an arbitrary scenario name, ticket, or workflow through
    this endpoint."""

    FEATURE = "feature"
    INCIDENT = "incident"


@router.post("/scenarios/{scenario}", response_model=DemoScenarioResult)
async def run_demo_scenario(scenario: DemoScenario, container: ApiContainer = Depends(get_container)) -> DemoScenarioResult:
    started = time.perf_counter()

    cached = container.demo_scenario_results.get(scenario.value)
    if cached is not None:
        logger.info("api.demo op=run_demo_scenario scenario=%s cached=true", scenario.value)
        return DemoScenarioResult(**cached, already_seeded=True)

    harness = DemoHarness(
        signal_repo=container.signal_repo,
        detection_repo=container.detection_repo,
        resolution_repo=container.resolution_repo,
        review_repo=container.review_repo,
        workflow_repo=container.workflow_repo,
        artifact_repo=container.artifact_repo,
        execution_repo=container.execution_repo,
        decision_repo=container.decision_repo,
        verification_repo=container.verification_repo,
    )
    summary = await harness.run_feature_flow() if scenario == DemoScenario.FEATURE else await harness.run_incident_flow()

    payload = {
        "scenario": summary.scenario,
        "workflow_id": summary.workflow_id,
        "signal_ids": summary.signal_ids,
        "detection_id": summary.detection_id,
        "review_id": summary.review_id,
        "resolution_id": summary.resolution_id,
        "verification_status": summary.verification_status,
    }
    container.demo_scenario_results[scenario.value] = payload

    logger.info(
        "api.demo op=run_demo_scenario scenario=%s workflow_id=%s detection_id=%s duration_ms=%.1f",
        scenario.value,
        summary.workflow_id,
        summary.detection_id,
        (time.perf_counter() - started) * 1000,
    )
    return DemoScenarioResult(**payload, already_seeded=False)
