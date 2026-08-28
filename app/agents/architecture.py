"""Architecture agent.

Two things live here, deliberately, during this migration — same pattern as
app/agents/planning.py (Level 1.5):

1. `architecture_agent` — the original native ADK LlmAgent (unchanged),
   still used by the legacy SequentialAgent pipeline
   (app/orchestrator/pipeline.py). Reads state["plan"] directly. Preserved
   exactly so the legacy pipeline's import/behavior doesn't break.

2. `ArchitectureAgent` — the new QuipuAgent-native wrapper (Level 1.6). This
   is what the future orchestrator invokes. It consumes the Planning result
   through the persisted PlanArtifact (via ArtifactGateway), not raw ADK
   session state — session state is still used internally by its own ADK
   adapter to render the instruction, but the artifact fetch is the
   authoritative inter-agent contract.

See docs/architecture/architecture_agent.md for the full current/target
architecture and the RBAC/metrics bridge (identical reasoning to Planning's).
"""

import json
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent_runtime.base import QuipuAgent
from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.identity import AgentIdentity
from app.agents.planning import PlanOutput, Risk, _non_empty, _tool_capability_gate, _track_usage_metrics
from app.config import get_settings
from app.core.db_hooks import stage_completed, stage_started
from app.core.metrics import RunMetrics
from app.core.observability import get_logger
from app.core.rbac import STAGE_ROLES, Permission
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

logger = get_logger("quipu.agent.architecture")
settings = get_settings()


class Component(BaseModel):
    name: str
    responsibility: str

    _validate_name = field_validator("name")(_non_empty)
    _validate_responsibility = field_validator("responsibility")(_non_empty)


class TaskDesign(BaseModel):
    task_id: str
    approach: str
    files: list[str] = Field(default_factory=list)

    _validate_task_id = field_validator("task_id")(_non_empty)
    _validate_approach = field_validator("approach")(_non_empty)


class ArchitectureOutput(BaseModel):
    design_summary: str
    components: list[Component]
    data_model_changes: list[str] = Field(default_factory=list)
    api_contracts: list[str] = Field(default_factory=list)
    task_designs: list[TaskDesign]
    risks: list[Risk]

    _validate_design_summary = field_validator("design_summary")(_non_empty)

    @field_validator("components")
    @classmethod
    def _at_least_one_component(cls, value: list[Component]) -> list[Component]:
        if not value:
            raise ValueError("components must not be empty")
        return value

    @field_validator("task_designs")
    @classmethod
    def _at_least_one_task_design(cls, value: list[TaskDesign]) -> list[TaskDesign]:
        if not value:
            raise ValueError("task_designs must not be empty")
        return value

    @model_validator(mode="after")
    def _task_designs_unique(self) -> "ArchitectureOutput":
        ids = [td.task_id for td in self.task_designs]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate task_design task_ids: {sorted(duplicates)}")
        return self


def validate_task_coverage(architecture: ArchitectureOutput, plan: dict) -> None:
    """Cross-checks task_designs against the plan's actual task ids.

    Not a pydantic validator because it needs the plan, which lives outside
    ArchitectureOutput's own schema. Application-level, authoritative — the
    LLM is never trusted to have followed the one-task-per-plan-task rule on
    its own; this is called explicitly after ArchitectureOutput validates,
    on both the legacy and the new Quipu-native path.
    """
    plan_task_ids = {task["id"] for task in plan.get("tasks", [])}
    design_task_ids = {td.task_id for td in architecture.task_designs}

    missing = plan_task_ids - design_task_ids
    unknown = design_task_ids - plan_task_ids
    errors = []
    if missing:
        errors.append(f"no task_design for plan task id(s): {sorted(missing)}")
    if unknown:
        errors.append(f"task_design references unknown plan task id(s): {sorted(unknown)}")
    if errors:
        raise ValueError("; ".join(errors))


def _build_instruction(context: ReadonlyContext) -> str:
    plan = context.state.get("plan")
    plan_json = json.dumps(plan, indent=2) if plan else "(no plan found in session state)"

    knowledge_note = ""
    if context.state.get("_knowledge_gateway") is not None:
        knowledge_note = (
            "\n\nYou also have query_enterprise_knowledge. If an enterprise "
            "architecture standard, approved technology, security/compliance "
            "requirement, or platform pattern is relevant, query it before "
            "finalizing the design. If nothing relevant is found, reason from "
            "the repository and task context — never fabricate an enterprise "
            "standard."
        )

    return f"""You are the Architecture agent in Quipu's SDLC pipeline.

The Planning agent has already produced this plan (the ordered tasks to be
designed):
{plan_json}

You have tools to inspect the actual repo this feature will be built in:
get_project_structure, search_files, search_code, read_file, get_dependencies.
Use them before designing — check real module boundaries and existing patterns
so the design fits this codebase, not a generic one.{knowledge_note}

Produce a technical design in this order:

1. design_summary — the overall technical approach in a few sentences.
2. components — each new or modified module/service and its responsibility.
3. data_model_changes — schema/model changes required, if any.
4. api_contracts — new or changed endpoints/interfaces, if any.
5. task_designs — exactly one entry per task id from the plan above. Each
   entry's task_id must match a plan task id exactly. Describe the approach
   and the specific files to touch for that task.
6. risks — technical risks in this design, each with a mitigation.

Return only the structured design."""


def _rbac_gate(callback_context: CallbackContext) -> None:
    STAGE_ROLES["architecture"].requires(Permission.READ_CODEBASE)


def _track_usage(callback_context: CallbackContext, llm_response: LlmResponse) -> None:
    """Legacy usage tracker — writes into app.core.metrics.RunMetrics.
    Still used by the legacy `architecture_agent` only."""
    usage = llm_response.usage_metadata
    if usage is None:
        return
    metrics: RunMetrics | None = callback_context.state.get("_metrics")
    cost_usd = (
        (usage.prompt_token_count or 0) / 1_000_000 * 1.25
        + (usage.candidates_token_count or 0) / 1_000_000 * 5.00
    )
    logger.info(
        "architecture llm usage prompt=%s completion=%s cost_usd=%.6f",
        usage.prompt_token_count,
        usage.candidates_token_count,
        cost_usd,
    )
    if metrics is not None:
        metrics.record("architecture", cost_usd=cost_usd, latency_ms=0.0)


# ---------------------------------------------------------------------------
# Legacy ADK agent — unchanged. Used by app/orchestrator/pipeline.py
# (SequentialAgent). Do not modify without checking that pipeline.
# ---------------------------------------------------------------------------

architecture_agent = LlmAgent(
    name="architecture",
    description="Turns a task plan into a technical design, one entry per task.",
    model=settings.gemini_model,
    instruction=_build_instruction,
    output_schema=ArchitectureOutput,
    output_key="architecture",
    tools=REPO_TOOLS,
    before_agent_callback=[_rbac_gate, stage_started("architecture")],
    after_agent_callback=[stage_completed("architecture", "architecture")],
    after_model_callback=_track_usage,
)


# ---------------------------------------------------------------------------
# New path (Level 1.6): an otherwise-identical LlmAgent that also has
# query_enterprise_knowledge, plus the shared capability gate/metrics tracker
# already built for Planning (reused, not duplicated).
# ---------------------------------------------------------------------------

_architecture_llm_agent = LlmAgent(
    name="architecture",
    description="Turns a task plan into a technical design, one entry per task.",
    model=settings.gemini_model,
    instruction=_build_instruction,
    output_schema=ArchitectureOutput,
    output_key="architecture",
    tools=REPO_TOOLS + KNOWLEDGE_TOOLS,
    before_tool_callback=_tool_capability_gate,
    after_model_callback=_track_usage_metrics,
)


class ArchitectureAgent(QuipuAgent):
    """Quipu-native Architecture Agent. Consumes the PlanArtifact (via
    ArtifactGateway, not raw session state), inspects the real repo,
    optionally consults enterprise knowledge, produces a validated
    ArchitectureOutput with exactly one TaskDesign per plan task, and
    persists the result as an ArchitectureArtifact. Never calls another
    agent — routing is the orchestrator's job.
    """

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id="architecture_agent",
            name="Architecture Agent",
            version="1.0.0",
            description="Turns a validated plan into a technical design, one entry per task.",
        )

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {
            AgentCapability.READ_TICKET,
            AgentCapability.READ_ARTIFACT,
            AgentCapability.QUERY_KNOWLEDGE,
            AgentCapability.READ_REPOSITORY,
            AgentCapability.WRITE_ARTIFACT,
            AgentCapability.CREATE_ARCHITECTURE,
        }

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        self.require_capability(AgentCapability.READ_TICKET)
        self.require_capability(AgentCapability.READ_ARTIFACT)
        self.require_capability(AgentCapability.READ_REPOSITORY)
        self.require_capability(AgentCapability.CREATE_ARCHITECTURE)

        # Smallest safe bridge (Level 1.5/1.6, see docs/architecture/architecture_agent.md
        # "Capability/RBAC model"): the legacy Permission enum still guards the
        # legacy ADK pipeline's `architecture_agent` via _rbac_gate; here,
        # checked once more since it maps to the same repository-access
        # concern, until Permission is fully retired in favor of AgentCapability.
        STAGE_ROLES["architecture"].requires(Permission.READ_CODEBASE)

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

        # --- Consume the Planning result through the artifact abstraction,
        # not context.state["plan"]. AgentInput.artifact_ids[0] is the
        # PlanArtifact's id by convention (the only input artifact Architecture
        # expects); a future orchestrator sets this when dispatching Architecture.
        if not agent_input.artifact_ids:
            return await _fail(
                "PLAN_ARTIFACT_MISSING", "AgentInput.artifact_ids is empty; no plan artifact reference given", ErrorCategory.VALIDATION
            )

        plan_artifact_id = agent_input.artifact_ids[0]
        plan_artifact = await context.artifacts.get(agent_input.workflow_id, plan_artifact_id)
        if plan_artifact is None:
            return await _fail(
                "PLAN_ARTIFACT_MISSING", f"no artifact '{plan_artifact_id}' found for workflow '{agent_input.workflow_id}'", ErrorCategory.VALIDATION
            )
        if plan_artifact.artifact_type != ArtifactType.PLAN:
            return await _fail(
                "PLAN_ARTIFACT_WRONG_TYPE",
                f"artifact '{plan_artifact_id}' has type '{plan_artifact.artifact_type}', expected '{ArtifactType.PLAN}'",
                ErrorCategory.VALIDATION,
            )
        try:
            plan = PlanOutput.model_validate(plan_artifact.payload)
        except ValidationError as exc:
            return await _fail("PLAN_OUTPUT_INVALID", str(exc), ErrorCategory.VALIDATION)

        session_state: dict = {
            "plan": plan.model_dump(mode="json"),
            "workflow_id": agent_input.workflow_id,
            "_agent_name": self.identity.agent_id,
            "_capabilities": self.capabilities,
            "_metrics": metrics,
        }
        if AgentCapability.QUERY_KNOWLEDGE in self.capabilities:
            session_state["_knowledge_gateway"] = context.knowledge
        workspace_path = agent_input.context.get("workspace_path")
        if workspace_path:
            session_state["workspace_path"] = workspace_path

        runner = InMemoryRunner(agent=_architecture_llm_agent, app_name="quipu")
        session = await runner.session_service.create_session(
            app_name="quipu", user_id=agent_input.workflow_id, state=session_state
        )
        message = types.Content(role="user", parts=[types.Part(text="Begin architecture design.")])

        final_text = ""
        try:
            async for event in runner.run_async(user_id=agent_input.workflow_id, session_id=session.id, new_message=message):
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = event.content.parts[0].text
        except Exception as exc:  # Gemini/ADK/tool failure — never fabricate a design.
            logger.exception("architecture agent LLM execution failed")
            return await _fail("ARCHITECTURE_LLM_FAILURE", str(exc), ErrorCategory.LLM_FAILURE)

        if not final_text.strip():
            return await _fail("ARCHITECTURE_EMPTY_RESPONSE", "model returned an empty response", ErrorCategory.LLM_FAILURE)

        try:
            architecture = ArchitectureOutput.model_validate_json(final_text)
        except ValidationError as exc:
            return await _fail("ARCHITECTURE_VALIDATION_FAILED", str(exc), ErrorCategory.VALIDATION)

        try:
            validate_task_coverage(architecture, plan.model_dump(mode="json"))
        except ValueError as exc:
            return await _fail("TASK_COVERAGE_INCOMPLETE", str(exc), ErrorCategory.VALIDATION)

        self.require_capability(AgentCapability.WRITE_ARTIFACT)
        artifact = Artifact(
            artifact_id=str(uuid.uuid4()),
            artifact_type=ArtifactType.ARCHITECTURE,
            created_by=self.identity.agent_id,
            parent_artifact_ids=[plan_artifact_id],
            payload=architecture.model_dump(mode="json"),
        )
        try:
            await context.artifacts.save(agent_input.workflow_id, artifact)
        except Exception as exc:
            logger.exception("architecture artifact persistence failed")
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
            messages=[architecture.design_summary],
            metrics=metrics,
        )
