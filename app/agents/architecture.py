"""Architecture agent — native ADK LlmAgent.

Reads the Planning stage's output from session state ("plan") and produces a
technical design, with one task_design entry per plan task for Coding to consume.
"""

import json

from pydantic import BaseModel, Field, field_validator, model_validator

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_response import LlmResponse

from app.agents.planning import Risk, _non_empty
from app.config import get_settings
from app.core.db_hooks import stage_completed, stage_started
from app.core.metrics import RunMetrics
from app.core.observability import get_logger
from app.core.rbac import STAGE_ROLES, Permission
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
    ArchitectureOutput's own schema — call this from the runner/orchestrator
    after both are available.
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
    return f"""You are the Architecture agent in Quipu's SDLC pipeline.

You have tools to inspect the actual repo this feature will be built in:
get_project_structure, search_files, search_code, read_file, get_dependencies.
Use them before designing — check real module boundaries and existing patterns
so the design fits this codebase, not a generic one.

Here is the plan from the Planning stage:
{plan_json}

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
