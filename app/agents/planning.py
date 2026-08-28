"""Planning agent.

Two things live here, deliberately, during this migration:

1. `planning_agent` — the original native ADK LlmAgent (unchanged), still
   used by the legacy SequentialAgent pipeline (app/orchestrator/pipeline.py).
   Preserved exactly so the legacy pipeline's import/behavior doesn't break.

2. `PlanningAgent` — the new QuipuAgent-native wrapper (Level 1.5). This is
   what the future orchestrator invokes. It owns identity/capabilities/
   lifecycle/persistence via app.agent_runtime + app.persistence, and
   delegates the actual reasoning/tool-calling/structured-output work to its
   own internal ADK LlmAgent (`_planning_llm_agent`) — Gemini and ADK stay the
   execution mechanism, not the owner of Quipu business state.

See docs/architecture/planning_agent.md for the full current/target
architecture, the RBAC bridge, and why two LlmAgent instances exist for now.
"""

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
from app.agent_runtime.capabilities import AgentCapability, check_capability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.identity import AgentIdentity
from app.config import get_settings
from app.core.db_hooks import stage_completed, stage_started
from app.core.jira_client import JiraClient
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
from app.tools.jira_tools import JIRA_TOOLS
from app.tools.knowledge_tools import KNOWLEDGE_TOOLS
from app.tools.repo_tools import REPO_TOOLS

logger = get_logger("quipu.agent.planning")
settings = get_settings()


def _non_empty(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("must not be empty")
    return value.strip()


class AffectedComponent(BaseModel):
    name: str
    reason: str

    _validate_name = field_validator("name")(_non_empty)
    _validate_reason = field_validator("reason")(_non_empty)


class PlanTask(BaseModel):
    id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    jira_key: str | None = None

    _validate_id = field_validator("id")(_non_empty)
    _validate_description = field_validator("description")(_non_empty)

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "PlanTask":
        if self.id in self.depends_on:
            raise ValueError(f"task '{self.id}' cannot depend on itself")
        return self


class Risk(BaseModel):
    description: str
    mitigation: str

    _validate_description = field_validator("description")(_non_empty)
    _validate_mitigation = field_validator("mitigation")(_non_empty)


class PlanOutput(BaseModel):
    feature_summary: str
    architecture_notes: str
    affected_components: list[AffectedComponent]
    tasks: list[PlanTask]
    dependencies: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str]
    risks: list[Risk]

    _validate_feature_summary = field_validator("feature_summary")(_non_empty)
    _validate_architecture_notes = field_validator("architecture_notes")(_non_empty)

    @field_validator("affected_components")
    @classmethod
    def _at_least_one_component(cls, value: list[AffectedComponent]) -> list[AffectedComponent]:
        if not value:
            raise ValueError("affected_components must not be empty")
        return value

    @field_validator("tasks")
    @classmethod
    def _at_least_one_task(cls, value: list[PlanTask]) -> list[PlanTask]:
        if not value:
            raise ValueError("tasks must not be empty")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _acceptance_criteria_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("acceptance_criteria must not be empty")
        return [_non_empty(v) for v in value]

    @model_validator(mode="after")
    def _task_ids_unique_and_resolvable(self) -> "PlanOutput":
        ids = [task.id for task in self.tasks]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate task ids: {sorted(duplicates)}")

        id_set = set(ids)
        for task in self.tasks:
            unknown = [dep for dep in task.depends_on if dep not in id_set]
            if unknown:
                raise ValueError(f"task '{task.id}' depends on unknown task id(s): {unknown}")
        return self


# ---------------------------------------------------------------------------
# Shared instruction builder. include_jira_step distinguishes the legacy
# behaviour (LLM calls create_story itself, mid-reasoning) from the new
# behaviour (Jira creation moves to deterministic post-validation code — see
# _create_jira_stories below and docs/architecture/planning_agent.md, "Jira
# integration": the old inline-tool-call approach let stories get created
# before the plan was known to be valid, which the new path fixes).
# ---------------------------------------------------------------------------


def _build_instruction(include_jira_step: bool):
    def _instruction(context: ReadonlyContext) -> str:
        request_text = (
            context.state.get("ticket_summary")
            or context.state.get("feature_request")
            or "(no feature request or ticket found in session state)"
        )

        knowledge_note = ""
        if context.state.get("_knowledge_gateway") is not None:
            knowledge_note = (
                "\n\nYou also have query_enterprise_knowledge to look up relevant "
                "architecture patterns, compliance rules, technology standards, or "
                "historical project context. Use it when it would materially improve "
                "the plan — not for every task, and only with the knowledge_type "
                "values it tells you are allowed."
            )

        jira_section = ""
        if include_jira_step:
            jira_section = (
                "\n\nOnce the task list is finalized, call create_story once per task "
                "(summary = a short title for the task, description = the task's full "
                "description) and record the returned issue key in that task's "
                "jira_key field. Do this for every task — one Jira story per task, "
                "no exceptions."
            )

        return f"""You are the Planning agent in Quipu's SDLC pipeline.

You have tools to inspect the actual repo this feature will be built in:
get_project_structure, search_files, search_code, read_file, get_dependencies.
Use them before writing the plan — check real module/file names, existing
patterns, and declared dependencies so the plan is specific to this codebase,
not generic. Don't guess a structure you haven't looked at.{knowledge_note}

The feature request / ticket for this plan:
{request_text}

Work through it in this order and fill every field:

1. feature_summary — restate the feature in one or two sentences.
2. architecture_notes — how this fits the *existing* system, referencing real
   files/modules you found via the tools.
3. affected_components — each real system/module touched, and why.
4. tasks — ordered, concrete engineering tasks, each small enough for a
   single Architecture/Coding pass. Reference other tasks by id in depends_on.
5. dependencies — external dependencies outside this task list (other teams,
   services, infra, approvals), informed by get_dependencies where relevant.
6. acceptance_criteria — concrete, testable conditions for "done".
7. risks — what could go wrong, each with a mitigation.{jira_section}

Return only the structured plan."""

    return _instruction


def _rbac_gate(callback_context: CallbackContext) -> None:
    STAGE_ROLES["planning"].requires(Permission.READ_KNOWLEDGE_BASE)


def _track_usage(callback_context: CallbackContext, llm_response: LlmResponse) -> None:
    """Legacy usage tracker — writes into app.core.metrics.RunMetrics.
    Still used by the legacy `planning_agent` only; see _track_usage_metrics
    for the AgentMetrics-based equivalent the new path uses."""
    usage = llm_response.usage_metadata
    if usage is None:
        return
    metrics: RunMetrics | None = callback_context.state.get("_metrics")
    latency_ms = 0.0  # ADK does not surface per-call latency on the response; tracked via span in the caller.
    cost_usd = (
        (usage.prompt_token_count or 0) / 1_000_000 * 1.25
        + (usage.candidates_token_count or 0) / 1_000_000 * 5.00
    )
    logger.info(
        "planning llm usage prompt=%s completion=%s cost_usd=%.6f",
        usage.prompt_token_count,
        usage.candidates_token_count,
        cost_usd,
    )
    if metrics is not None:
        metrics.record("planning", cost_usd=cost_usd, latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# Legacy ADK agent — unchanged. Used by app/orchestrator/pipeline.py
# (SequentialAgent). Do not modify without checking that pipeline.
# ---------------------------------------------------------------------------

planning_agent = LlmAgent(
    name="planning",
    description="Breaks a detected feature into an ordered task plan.",
    model=settings.gemini_model,
    instruction=_build_instruction(include_jira_step=True),
    output_schema=PlanOutput,
    output_key="plan",
    tools=REPO_TOOLS + JIRA_TOOLS,
    before_agent_callback=[_rbac_gate, stage_started("planning")],
    after_agent_callback=[stage_completed("planning", "plan")],
    after_model_callback=_track_usage,
)


# ---------------------------------------------------------------------------
# New path (Level 1.5): a second, otherwise-identical LlmAgent WITHOUT the
# Jira tool — Jira creation happens in PlanningAgent._perform, after
# PlanOutput validates, not during the model's own reasoning turn. Also has a
# before_tool_callback enforcing Quipu capabilities at actual tool-call time,
# not just as an upfront check the LLM could route around.
# ---------------------------------------------------------------------------

_TOOL_CAPABILITY_MAP = {
    "search_files": AgentCapability.READ_REPOSITORY,
    "read_file": AgentCapability.READ_REPOSITORY,
    "search_code": AgentCapability.READ_REPOSITORY,
    "get_project_structure": AgentCapability.READ_REPOSITORY,
    "get_dependencies": AgentCapability.READ_REPOSITORY,
    "query_enterprise_knowledge": AgentCapability.QUERY_KNOWLEDGE,
}


def _tool_capability_gate(tool, args: dict, tool_context) -> None:
    required = _TOOL_CAPABILITY_MAP.get(tool.name)
    if required is None:
        return None
    granted: set[AgentCapability] = tool_context.state.get("_capabilities", set())
    check_capability("planning_agent", granted, required)
    return None


def _track_usage_metrics(callback_context: CallbackContext, llm_response: LlmResponse) -> None:
    usage = llm_response.usage_metadata
    if usage is None:
        return
    metrics: AgentMetrics | None = callback_context.state.get("_metrics")
    if metrics is None:
        return
    prompt_tokens = usage.prompt_token_count or 0
    completion_tokens = usage.candidates_token_count or 0
    cost_usd = prompt_tokens / 1_000_000 * 1.25 + completion_tokens / 1_000_000 * 5.00

    metrics.prompt_tokens = (metrics.prompt_tokens or 0) + prompt_tokens
    metrics.completion_tokens = (metrics.completion_tokens or 0) + completion_tokens
    metrics.total_tokens = (metrics.total_tokens or 0) + prompt_tokens + completion_tokens
    metrics.cost_usd = (metrics.cost_usd or 0.0) + cost_usd
    logger.info(
        "planning llm usage prompt=%s completion=%s cost_usd=%.6f",
        prompt_tokens,
        completion_tokens,
        cost_usd,
    )


_planning_llm_agent = LlmAgent(
    name="planning",
    description="Breaks a detected feature into an ordered task plan.",
    model=settings.gemini_model,
    instruction=_build_instruction(include_jira_step=False),
    output_schema=PlanOutput,
    output_key="plan",
    tools=REPO_TOOLS + KNOWLEDGE_TOOLS,
    before_tool_callback=_tool_capability_gate,
    after_model_callback=_track_usage_metrics,
)


def _create_jira_stories(plan: PlanOutput) -> PlanOutput:
    """Deterministic, runs only after PlanOutput has already validated —
    the required sequence is reason -> construct -> validate -> create Jira
    stories -> populate jira_key -> final PlanOutput. One story per task, no
    exceptions. Raises on the first failure rather than silently leaving a
    task's jira_key unset and pretending the story exists.
    """
    client = JiraClient()
    updated_tasks = []
    for task in plan.tasks:
        result = client.create_story(summary=task.description[:200], description=task.description)
        updated_tasks.append(task.model_copy(update={"jira_key": result["key"]}))
    return plan.model_copy(update={"tasks": updated_tasks})


class PlanningAgent(QuipuAgent):
    """Quipu-native Planning Agent. Understands a ticket, inspects the real
    repo, optionally consults enterprise knowledge, produces a validated
    PlanOutput, creates one Jira story per task, and persists the result as a
    PlanArtifact. Never calls another agent — routing is the orchestrator's job.
    """

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id="planning_agent",
            name="Planning Agent",
            version="1.0.0",
            description="Breaks a ticket into a validated, ordered implementation plan.",
        )

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {
            AgentCapability.READ_TICKET,
            AgentCapability.QUERY_KNOWLEDGE,
            AgentCapability.READ_REPOSITORY,
            AgentCapability.READ_ARTIFACT,
            AgentCapability.WRITE_ARTIFACT,
            AgentCapability.CREATE_PLAN,
            AgentCapability.WRITE_JIRA,
        }

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        self.require_capability(AgentCapability.READ_TICKET)
        self.require_capability(AgentCapability.CREATE_PLAN)
        self.require_capability(AgentCapability.READ_REPOSITORY)

        # Smallest safe bridge (Level 1.5, see docs/architecture/planning_agent.md
        # "Capability/RBAC model"): the legacy Permission enum still guards the
        # legacy ADK pipeline's `planning_agent` via _rbac_gate; here, checked
        # once more since it maps to the same enterprise-knowledge-access
        # concern, until Permission is fully retired in favor of AgentCapability.
        STAGE_ROLES["planning"].requires(Permission.READ_KNOWLEDGE_BASE)

        execution = AgentExecution(
            execution_id=agent_input.execution_id,
            workflow_id=agent_input.workflow_id,
            agent_name=self.identity.agent_id,
            status=WorkflowStatus.RUNNING,
        )
        if context.executions is not None:
            await context.executions.create(execution)

        metrics = AgentMetrics(execution_id=agent_input.execution_id)
        session_state: dict = {
            "ticket_summary": f"{agent_input.ticket.title}\n\n{agent_input.ticket.description}",
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

        runner = InMemoryRunner(agent=_planning_llm_agent, app_name="quipu")
        session = await runner.session_service.create_session(
            app_name="quipu", user_id=agent_input.workflow_id, state=session_state
        )
        message = types.Content(role="user", parts=[types.Part(text="Begin planning.")])

        final_text = ""
        try:
            async for event in runner.run_async(user_id=agent_input.workflow_id, session_id=session.id, new_message=message):
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = event.content.parts[0].text
        except Exception as exc:  # Gemini/ADK/tool failure — never fabricate a plan.
            logger.exception("planning agent LLM execution failed")
            return await _fail("PLANNING_LLM_FAILURE", str(exc), ErrorCategory.LLM_FAILURE)

        if not final_text.strip():
            return await _fail("PLANNING_EMPTY_RESPONSE", "model returned an empty response", ErrorCategory.LLM_FAILURE)

        try:
            plan = PlanOutput.model_validate_json(final_text)
        except ValidationError as exc:
            return await _fail("PLAN_VALIDATION_FAILED", str(exc), ErrorCategory.VALIDATION)

        self.require_capability(AgentCapability.WRITE_JIRA)
        try:
            plan = _create_jira_stories(plan)
        except Exception as exc:
            logger.exception("Jira story creation failed")
            return await _fail("JIRA_STORY_CREATION_FAILED", str(exc), ErrorCategory.TOOL_FAILURE)

        self.require_capability(AgentCapability.WRITE_ARTIFACT)
        artifact = Artifact(
            artifact_id=str(uuid.uuid4()),
            artifact_type=ArtifactType.PLAN,
            created_by=self.identity.agent_id,
            parent_artifact_ids=list(agent_input.artifact_ids),
            payload=plan.model_dump(mode="json"),
        )
        await context.artifacts.save(agent_input.workflow_id, artifact)

        execution.status = WorkflowStatus.COMPLETED
        execution.completed_at = datetime.utcnow()
        execution.output_artifact_ids = [artifact.artifact_id]
        if context.executions is not None:
            await context.executions.update(execution)

        return AgentOutput(
            execution_id=agent_input.execution_id,
            status=WorkflowStatus.COMPLETED,
            artifacts=[artifact],
            messages=[plan.feature_summary],
            metrics=metrics,
        )
