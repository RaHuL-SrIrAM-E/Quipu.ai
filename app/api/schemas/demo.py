"""Demo-scenario-seeding response schema — see
app/api/routes/demo.py."""

from pydantic import BaseModel


class DemoScenarioResult(BaseModel):
    scenario: str
    workflow_id: str | None
    signal_ids: list[str]
    detection_id: str | None
    review_id: str | None
    resolution_id: str | None
    verification_status: str
    already_seeded: bool
