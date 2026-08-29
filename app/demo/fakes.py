"""Demo-only fake infrastructure — the injectable boundary the task asks
for ("provide an explicit demo/test adapter or injectable model boundary
rather than changing production behavior").

Nothing here is production code and nothing here is imported by any
app.agents/app.orchestration/app.persistence module. It exists purely so
app/demo/harness.py (and the real Quipu agents it invokes unmodified) can
run without live Gemini, Jira, Cloud Monitoring, Cloud Logging, or Cloud
Run credentials. The ORCHESTRATION LOGIC itself is never faked — only the
external system boundaries each agent already isolates behind an
injectable client/runner (the same seam tests/test_orchestration.py and
tests/test_incident_remediation.py already use).

Every fake here mirrors the exact shape of the real thing it stands in
for:
  - FakeAdkRunner   -> google.adk.runners.InMemoryRunner (module-level
                       substitution, same mechanism pytest's monkeypatch
                       fixture uses — see app/demo/patching.py)
  - FakeJiraClient  -> app.core.jira_client.JiraClient
  - FakeCloudRunDeployer -> the `client` DeploymentAgent's CloudRunDeployer
                       accepts via constructor injection
  - FakeCloudMonitoringClient / FakeCloudLoggingClient -> the `monitoring_client`/
                       `logging_client` MonitoringAgent accepts via
                       constructor injection (no module-patch needed there)
  - FakeKnowledgeGateway / FakeToolGateway -> the Protocols every
                       AgentContext already requires
"""

from datetime import datetime, timezone

from google.genai import types

from app.core.cloud_logging_client import LogEntryResult
from app.core.cloud_monitoring_client import MetricPoint
from app.tools.codegen_tools import write_file
from app.tools.deployment_tools import deploy_cloud_run
from app.tools.testing_tools import run_tests

# ---- Fixture data: valid structured outputs for each reasoning agent ------
# Shaped exactly like tests/test_orchestration.py's fixtures — the demo and
# the test suite exercise the identical Pydantic output schemas.

VALID_PLAN = {
    "feature_summary": "Add CSV/Excel export to the reports page.",
    "architecture_notes": "Extend the existing report renderer with a new export format.",
    "affected_components": [{"name": "reports", "reason": "add an export action"}],
    "tasks": [{"id": "t1", "description": "implement Excel export for the reports page", "depends_on": []}],
    "dependencies": [],
    "acceptance_criteria": ["a user can export the current report to Excel"],
    "risks": [{"description": "large reports may be slow to export", "mitigation": "stream rows instead of buffering"}],
}

VALID_ARCHITECTURE = {
    "design_summary": "Add an ExcelExporter alongside the existing report renderer.",
    "components": [{"name": "ExcelExporter", "responsibility": "serializes a report to .xlsx"}],
    "data_model_changes": [],
    "api_contracts": [],
    "task_designs": [{"task_id": "t1", "approach": "add ExcelExporter and wire it into the export action", "files": ["src/export.py"]}],
    "risks": [{"description": "xlsx library adds a new dependency", "mitigation": "vendor a small, audited library"}],
}

VALID_CODEGEN = {
    "summary": "Implemented ExcelExporter.",
    "modified_files": [],
    "created_files": ["src/export.py"],
    "deleted_files": [],
    "changes": [{"path": "src/export.py", "change_type": "created", "description": "Excel exporter"}],
    "implementation_notes": "",
    "unresolved_items": [],
    "tests_to_run": ["test_export.py"],
}

VALID_TESTING_PASS = {
    "summary": "All tests pass.",
    "overall_status": "passed",
    "test_strategy": "regression",
    "targeted_tests": [],
    "regression_tests": ["test_export.py"],
    "failures": [],
    "environment_errors": [],
    "coverage_summary": "",
    "recommendations": [],
}


def testing_output_with_failures(failures: list[dict]) -> dict:
    return {**VALID_TESTING_PASS, "overall_status": "failed", "failures": failures}


VALID_DEPLOYMENT = {
    "deployment_summary": "Deployed the Excel exporter to Cloud Run.",
    "target_platform": "cloud_run",
    "environment": "production",
    "service_name": "quipu-demo",
    "region": "us-central1",
    "strategy": "revision",
    "configuration": {"image_tag": "v1", "cpu": "1", "memory": "512Mi", "min_instances": 0, "max_instances": 2},
    "pre_deployment_checks": ["tests passed"],
    "rollback_strategy": "revert to the previous Cloud Run revision",
    "risks": [],
}


def detection_output(
    *,
    detection_type: str,
    title: str,
    summary: str,
    rationale: str,
    subject: str,
    supporting_signal_ids: list[str],
    confidence: float = 0.9,
    severity: str | None = None,
) -> dict:
    return {
        "detection_type": detection_type,
        "title": title,
        "summary": summary,
        "rationale": rationale,
        "confidence": confidence,
        "severity": severity,
        "subject": subject,
        "supporting_signal_ids": supporting_signal_ids,
        "knowledge_references": [],
    }


def resolution_proposal(
    *,
    strategy: str,
    supporting_signal_ids: list[str],
    root_cause_confidence: float = 0.9,
    risk: str = "low",
    target_agent: str | None = None,
    rollback_target: str | None = None,
) -> dict:
    return {
        "diagnosis_summary": "Application defect introduced by the most recent deployment.",
        "probable_root_cause": "Unhandled exception in the request handler added by the last change.",
        "root_cause_confidence": root_cause_confidence,
        "root_cause_candidates": [],
        "remediation_strategy": strategy,
        "remediation_rationale": "Application-error signals correlate with the deployment event.",
        "expected_outcome": "Error rate returns to baseline after the fix deploys.",
        "verification_strategy": "Re-run the test suite and monitor the error rate after deployment.",
        "risk": risk,
        "severity": "critical",
        "escalation_recommended": False,
        "target_agent": target_agent,
        "rollback_target": rollback_target,
        "supporting_signal_ids": supporting_signal_ids,
        "supporting_artifact_ids": [],
        "knowledge_references": [],
    }


# ---- ADK runner fakes -------------------------------------------------------


class _FakeEvent:
    def __init__(self, text: str):
        self.content = types.Content(role="model", parts=[types.Part(text=text)])

    def is_final_response(self) -> bool:
        return True


class _FakeSession:
    id = "demo-session"


class _CapturingSessionService:
    def __init__(self):
        self.captured_state: dict = {}

    async def create_session(self, **kwargs):
        self.captured_state = kwargs.get("state", {})
        return _FakeSession()


class _FakeToolContext:
    def __init__(self, state: dict):
        self.state = state


def make_plain_runner(final_text: str):
    """A fake google.adk.runners.InMemoryRunner that yields exactly one
    final-response event with `final_text` — for agents whose only job in a
    given step is to return structured output (Planning, Architecture,
    Detecting, IncidentResolution)."""

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


def make_codegen_runner(final_text: str, *, path: str = "src/export.py", content: str = "def export_excel():\n    pass\n"):
    """Actually calls the real write_file tool so CodegenAgent's own
    ground-truth filesystem check has something real to find — the same
    pattern tests/test_orchestration.py uses."""

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                ctx = _FakeToolContext(self.session_service.captured_state)
                write_file(path, content, ctx)
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


def make_testing_runner(final_text: str, mode: str = "regression"):
    """Actually calls the real run_tests tool (a genuine subprocess pytest
    run against the demo workspace) — TestingAgent's evidence-first
    overwrite of overall_status is exercised for real, not faked."""

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                ctx = _FakeToolContext(self.session_service.captured_state)
                run_tests(mode, [], [], ctx)
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


class FakeCloudRunDeployer:
    """Injected via session_state["_cloud_run_deployer"] — the exact seam
    app/tools/deployment_tools.py::deploy_cloud_run already reads before
    lazily constructing a real CloudRunDeployer."""

    def __init__(self, succeed: bool = True):
        self._succeed = succeed

    async def deploy(self, **kwargs):
        from app.core.cloud_run_client import CloudRunDeployResult

        return CloudRunDeployResult(
            status="succeeded" if self._succeed else "failed",
            service_name=kwargs["service_name"],
            project="demo-project",
            region=kwargs["region"],
            revision=f"{kwargs['service_name']}-00001-demo" if self._succeed else None,
            uri=f"https://{kwargs['service_name']}-demo.a.run.app" if self._succeed else None,
            message="" if self._succeed else "container failed to start",
            deployed_at=datetime.now(timezone.utc),
        )


def make_deployment_runner(final_text: str, succeed: bool = True, service_name: str = "quipu-demo"):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                state = self.session_service.captured_state
                state["_cloud_run_deployer"] = FakeCloudRunDeployer(succeed=succeed)
                ctx = _FakeToolContext(state)
                await deploy_cloud_run(
                    service_name=service_name,
                    region="us-central1",
                    environment="production",
                    image_tag="v1",
                    cpu="1",
                    memory="512Mi",
                    min_instances=0,
                    max_instances=2,
                    tool_context=ctx,
                )
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


class FakeCloudRunSettings:
    cloud_run_image_registry = "gcr.io/demo-project"
    cloud_run_allowed_regions = ["us-central1"]
    cloud_run_allowed_environments = ["development", "staging", "production"]
    cloud_run_max_instances_ceiling = 10


# ---- Non-ADK infrastructure fakes -------------------------------------------


class FakeJiraClient:
    def __init__(self):
        self.created = []

    def create_story(self, summary: str, description: str) -> dict:
        key = f"DEMO-{len(self.created) + 1}"
        self.created.append({"key": key, "summary": summary})
        return {"key": key, "url": f"https://demo.atlassian.net/browse/{key}"}


class FakeKnowledgeGateway:
    async def search(self, request):
        return []


class FakeToolGateway:
    async def execute(self, request):
        raise NotImplementedError("demo scenarios do not exercise generic tool execution")


class FakeCloudMonitoringClient:
    """Stands in for app.core.cloud_monitoring_client.CloudMonitoringClient
    — MonitoringAgent accepts this via constructor injection, the same
    seam tests/test_monitoring_agent.py uses."""

    def __init__(self, *, error_rate: float = 0.0, latency_ms: float = 120.0):
        self._error_rate = error_rate
        self._latency_ms = latency_ms

    async def query_request_count_by_response_class(self, *, service_name, region, window_minutes):
        from datetime import timedelta

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=window_minutes)
        total = 1000
        errors = int(total * self._error_rate)
        return [
            MetricPoint(label="2xx", value=total - errors, window_start=start, window_end=end),
            MetricPoint(label="5xx", value=errors, window_start=start, window_end=end),
        ]

    async def query_latency_p99(self, *, service_name, region, window_minutes):
        from datetime import timedelta

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=window_minutes)
        return MetricPoint(label="p99_latency_ms", value=self._latency_ms, window_start=start, window_end=end)

    async def query_instance_count_by_state(self, *, service_name, region, window_minutes):
        from datetime import timedelta

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=window_minutes)
        return [MetricPoint(label="active", value=2, window_start=start, window_end=end)]


class FakeCloudLoggingClient:
    """Stands in for app.core.cloud_logging_client.CloudLoggingClient."""

    def __init__(self, entries: list[LogEntryResult] | None = None):
        self._entries = entries or []

    async def query_service_logs(self, *, service_name, region, window_minutes, min_severity, limit):
        return self._entries[:limit]
