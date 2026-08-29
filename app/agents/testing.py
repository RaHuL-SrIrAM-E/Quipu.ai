"""Testing agent — no legacy predecessor (like Codegen, this is
Quipu-native-only from the start).

Core principle: TOOLS PROVIDE FACTS, THE AGENT PROVIDES REASONING. Gemini
may analyze and classify test results, but it is never the authoritative
source for whether tests passed. That authority is app/tools/testing_tools.py
::run_tests — a controlled pytest invocation, never a shell — and
TestingAgent._perform() overrides whatever the model claims about
overall_status with what actually happened, every time.

See docs/architecture/testing_agent.md for the full design.
"""

import json
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError, field_validator

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent_runtime.base import QuipuAgent
from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.identity import AgentIdentity
from app.agents.codegen import CodegenOutput
from app.agents.planning import _non_empty, _tool_capability_gate, _track_usage_metrics
from app.config import get_settings
from app.core.observability import get_logger
from app.core.resilience.timeout import with_timeout
from app.domain import (
    AgentError,
    AgentExecution,
    AgentInput,
    AgentMetrics,
    AgentOutput,
    Artifact,
    ArtifactType,
    ErrorCategory,
    WorkflowStatus,
)
from app.tools.knowledge_tools import KNOWLEDGE_TOOLS
from app.tools.repo_tools import REPO_TOOLS
from app.tools.testing_tools import TESTING_TOOLS

logger = get_logger("quipu.agent.testing")
settings = get_settings()


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    NOT_RUN = "not_run"


class FailureClassification(StrEnum):
    CODE_DEFECT = "code_defect"
    TEST_DEFECT = "test_defect"
    ENVIRONMENT_FAILURE = "environment_failure"
    DEPENDENCY_FAILURE = "dependency_failure"
    UNKNOWN = "unknown"

    # Level 2.0: the orchestrator's decision policy needs to distinguish "the
    # code is wrong" from "the design is wrong" to route Codegen vs.
    # Architecture — added here (additive) rather than inventing a second
    # classification vocabulary in the orchestration layer.
    ARCHITECTURE_DEFECT = "architecture_defect"


class TestFailure(BaseModel):
    test_name: str
    classification: FailureClassification
    details: str = ""

    _validate_test_name = field_validator("test_name")(_non_empty)


class TestingOutput(BaseModel):
    summary: str
    overall_status: TestStatus
    test_strategy: str
    targeted_tests: list[str] = Field(default_factory=list)
    regression_tests: list[str] = Field(default_factory=list)
    failures: list[TestFailure] = Field(default_factory=list)
    environment_errors: list[str] = Field(default_factory=list)
    coverage_summary: str = ""
    recommendations: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)

    _validate_summary = field_validator("summary")(_non_empty)
    _validate_test_strategy = field_validator("test_strategy")(_non_empty)


def _build_instruction(context: ReadonlyContext) -> str:
    code_change = context.state.get("code_change")
    code_change_json = json.dumps(code_change, indent=2) if code_change else "(no code change found in session state)"

    knowledge_note = ""
    if context.state.get("_knowledge_gateway") is not None:
        knowledge_note = (
            "\n\nYou also have query_enterprise_knowledge — testing standards, "
            "regression policies, coverage requirements, security testing "
            "requirements, framework conventions, mandatory suites, historical "
            "failure patterns. Use it when deciding test strategy; don't call "
            "it reflexively."
        )

    return f"""You are Quipu's Testing Agent.

Your input is this CodeArtifact, produced by Codegen:
{code_change_json}

Your job is to determine and execute the appropriate tests, then analyze the
ACTUAL execution evidence. You do not decide whether tests passed — the
test runner does. You may explain and classify failures, but you cannot
override what actually happened.

You have tools to inspect the repo: get_project_structure, search_files,
search_code, read_file, get_dependencies. Inspect the changed files, nearby
tests, and test configuration/conventions before deciding what to
run.{knowledge_note}

Use run_tests(mode, test_paths, markers) to execute tests — mode is
"targeted" (specific test files/paths related to the change) or
"regression" (the full configured suite). No shell command is ever
available to you. You must call run_tests at least once — a testing
verdict without an actual execution is not acceptable; do not skip running
tests just because a change looks small.

After execution, classify any failures as one of: code_defect, test_defect,
architecture_defect (the design itself is wrong, not the implementation),
environment_failure, dependency_failure, unknown. Never modify application
code, never deploy, never invent a result the runner didn't report.

Return only the structured TestingOutput: summary, overall_status,
test_strategy, targeted_tests, regression_tests, failures,
environment_errors, coverage_summary, recommendations."""


_testing_llm_agent = LlmAgent(
    name="testing",
    description="Executes and analyzes tests against an approved code change.",
    model=settings.gemini_model,
    instruction=_build_instruction,
    output_schema=TestingOutput,
    output_key="testing",
    tools=REPO_TOOLS + KNOWLEDGE_TOOLS + TESTING_TOOLS,
    before_tool_callback=_tool_capability_gate,
    after_model_callback=_track_usage_metrics,
)


def _ground_truth_status(test_executions: list[dict]) -> TestStatus:
    """The real source of truth for overall_status — computed from actual
    run_tests results, never from the model's own claim. Any 'error' result
    (infrastructure/timeout/no-framework) wins over 'failed', which wins
    over 'passed' — a single broken run is enough to withhold a clean pass.
    """
    statuses = {execution["status"] for execution in test_executions}
    if "error" in statuses:
        return TestStatus.ERROR
    if "failed" in statuses:
        return TestStatus.FAILED
    return TestStatus.PASSED


class TestingAgent(QuipuAgent):
    """Quipu-native Testing Agent. Consumes the CodeArtifact (via
    ArtifactGateway), inspects the repo, optionally consults enterprise
    knowledge, executes tests through the capability-gated, shell-free
    run_tests tool, and produces a TestingOutput whose pass/fail verdict is
    always the actual execution result — never the model's opinion. Reports
    defects; never fixes them. Never calls another agent.
    """

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id="testing_agent",
            name="Testing Agent",
            version="1.0.0",
            description="Executes and analyzes tests against an approved code change.",
        )

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {
            AgentCapability.READ_ARTIFACT,
            AgentCapability.QUERY_KNOWLEDGE,
            AgentCapability.READ_REPOSITORY,
            AgentCapability.RUN_TESTS,
            AgentCapability.WRITE_ARTIFACT,
        }

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        self.require_capability(AgentCapability.READ_ARTIFACT)
        self.require_capability(AgentCapability.READ_REPOSITORY)
        self.require_capability(AgentCapability.RUN_TESTS)

        execution = AgentExecution(
            execution_id=agent_input.execution_id,
            workflow_id=agent_input.workflow_id,
            agent_name=self.identity.agent_id,
            status=WorkflowStatus.RUNNING,
        )
        if context.executions is not None:
            await context.executions.create(execution)

        metrics = AgentMetrics(execution_id=agent_input.execution_id)

        async def _fail(code: str, message: str, category: ErrorCategory, *, recoverable: bool = True) -> AgentOutput:
            error = AgentError(code=code, message=message, category=category, recoverable=recoverable, retryable=recoverable)
            execution.status = WorkflowStatus.FAILED
            execution.completed_at = datetime.utcnow()
            execution.error = error
            if context.executions is not None:
                await context.executions.update(execution)
            return AgentOutput(
                execution_id=agent_input.execution_id, status=WorkflowStatus.FAILED, errors=[error], metrics=metrics
            )

        # --- Consume the Codegen result through the artifact abstraction.
        if not agent_input.artifact_ids:
            return await _fail(
                "CODE_ARTIFACT_MISSING", "AgentInput.artifact_ids is empty; no code artifact reference given", ErrorCategory.VALIDATION
            )

        code_artifact_id = agent_input.artifact_ids[0]
        code_artifact = await context.artifacts.get(agent_input.workflow_id, code_artifact_id)
        if code_artifact is None:
            return await _fail(
                "CODE_ARTIFACT_MISSING",
                f"no artifact '{code_artifact_id}' found for workflow '{agent_input.workflow_id}'",
                ErrorCategory.VALIDATION,
            )
        if code_artifact.artifact_type != ArtifactType.CODE_CHANGE:
            return await _fail(
                "CODE_ARTIFACT_WRONG_TYPE",
                f"artifact '{code_artifact_id}' has type '{code_artifact.artifact_type}', expected '{ArtifactType.CODE_CHANGE}'",
                ErrorCategory.VALIDATION,
            )
        try:
            code_change = CodegenOutput.model_validate(code_artifact.payload)
        except ValidationError as exc:
            return await _fail("CODEGEN_OUTPUT_INVALID", str(exc), ErrorCategory.VALIDATION)

        workspace_path = agent_input.context.get("workspace_path")
        if not workspace_path:
            return await _fail(
                "TESTING_WORKSPACE_MISSING", "no repo checked out for this run (missing workspace_path)", ErrorCategory.INTERNAL
            )

        session_state: dict = {
            "code_change": code_change.model_dump(mode="json"),
            "workspace_path": workspace_path,
            "workflow_id": agent_input.workflow_id,
            "_agent_name": self.identity.agent_id,
            "_capabilities": self.capabilities,
            "_metrics": metrics,
            "_test_executions": [],
        }
        if AgentCapability.QUERY_KNOWLEDGE in self.capabilities:
            session_state["_knowledge_gateway"] = context.knowledge

        runner = InMemoryRunner(agent=_testing_llm_agent, app_name="quipu")
        session = await runner.session_service.create_session(
            app_name="quipu", user_id=agent_input.workflow_id, state=session_state
        )
        message = types.Content(role="user", parts=[types.Part(text="Begin testing.")])

        final_text = ""
        try:
            async def _consume_llm_response() -> None:
                nonlocal final_text
                async for event in runner.run_async(user_id=agent_input.workflow_id, session_id=session.id, new_message=message):
                    if event.is_final_response() and event.content and event.content.parts:
                        final_text = event.content.parts[0].text

            await with_timeout(_consume_llm_response(), settings.llm_call_timeout_seconds, operation="testing_agent_llm_call")
        except Exception as exc:  # Gemini/ADK/tool failure — never fabricate a result.
            logger.exception("testing agent LLM execution failed")
            return await _fail("TESTING_LLM_FAILURE", str(exc), ErrorCategory.LLM_FAILURE)

        if not final_text.strip():
            return await _fail("TESTING_EMPTY_RESPONSE", "model returned an empty response", ErrorCategory.LLM_FAILURE)

        try:
            testing_output = TestingOutput.model_validate_json(final_text)
        except ValidationError as exc:
            return await _fail("TESTING_VALIDATION_FAILED", str(exc), ErrorCategory.VALIDATION)

        # --- Evidence-first: a verdict requires actual execution evidence,
        # and the verdict is computed from that evidence — never from
        # whatever testing_output.overall_status claims.
        test_executions: list[dict] = session_state["_test_executions"]
        if not test_executions:
            return await _fail(
                "NO_TESTS_EXECUTED", "model produced a testing verdict without ever calling run_tests", ErrorCategory.VALIDATION
            )

        ground_truth_status = _ground_truth_status(test_executions)
        testing_output = testing_output.model_copy(update={"overall_status": ground_truth_status})

        self.require_capability(AgentCapability.WRITE_ARTIFACT)
        artifact = Artifact(
            artifact_id=str(uuid.uuid4()),
            artifact_type=ArtifactType.TEST_RESULT,
            created_by=self.identity.agent_id,
            parent_artifact_ids=[code_artifact_id],
            payload={**testing_output.model_dump(mode="json"), "raw_test_executions": test_executions},
        )
        try:
            await context.artifacts.save(agent_input.workflow_id, artifact)
        except Exception as exc:
            logger.exception("testing artifact persistence failed")
            return await _fail("ARTIFACT_PERSISTENCE_FAILED", str(exc), ErrorCategory.INTERNAL)

        execution.status = WorkflowStatus.COMPLETED
        execution.completed_at = datetime.utcnow()
        execution.output_artifact_ids = [artifact.artifact_id]
        if context.executions is not None:
            await context.executions.update(execution)

        return AgentOutput(
            execution_id=agent_input.execution_id,
            status=WorkflowStatus.COMPLETED,
            artifacts=[artifact],
            messages=[testing_output.summary],
            metrics=metrics,
        )
