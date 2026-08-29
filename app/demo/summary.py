"""DemoSummary — the machine-readable result of one demo scenario run.
Every field is populated from real, persisted domain state the harness
inspected after the fact (see app/demo/verify.py) — never asserted from
assumed success."""

from typing import Any

from pydantic import BaseModel, Field


class StepEvidence(BaseModel):
    """One inspected step in the scenario — a named checkpoint plus the
    concrete evidence the verifier found for it."""

    name: str
    passed: bool
    detail: str


class DemoSummary(BaseModel):
    scenario: str
    workflow_id: str | None = None
    signal_ids: list[str] = Field(default_factory=list)
    detection_id: str | None = None
    review_id: str | None = None
    ticket_id: str | None = None
    resolution_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    stages_executed: list[str] = Field(default_factory=list)
    final_status: str | None = None
    remediation_outcome: str | None = None
    escalated: bool = False
    verification_status: str = "unverified"  # "passed" | "failed"
    steps: list[StepEvidence] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    def record(self, name: str, passed: bool, detail: str) -> None:
        self.steps.append(StepEvidence(name=name, passed=passed, detail=detail))

    def finalize(self) -> None:
        self.verification_status = "passed" if all(s.passed for s in self.steps) else "failed"
