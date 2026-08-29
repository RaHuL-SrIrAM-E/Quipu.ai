"""End-to-end Quipu demo harness (see docs/architecture/end_to_end_demo.md).

Demo-only infrastructure — nothing under app/demo/ is imported by any
production module (app.agents/app.orchestration/app.persistence/...).
DemoHarness wires the real domain/agent-runtime/orchestration layer
together with fake external-system boundaries (ADK runner substitution,
fake Jira/Cloud Monitoring/Cloud Logging/Cloud Run clients) so both
end-to-end journeys run deterministically, without live credentials.
"""

from app.demo.harness import DemoHarness
from app.demo.summary import DemoSummary, StepEvidence

__all__ = ["DemoHarness", "DemoSummary", "StepEvidence"]
