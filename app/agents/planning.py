"""Planning agent — native ADK LlmAgent.

Breaks a feature request (plus any feature_detection state) into an ordered
task plan for the Architecture stage to consume.
"""

from pydantic import BaseModel, Field, field_validator, model_validator

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse

from app.config import get_settings
from app.core.metrics import RunMetrics
from app.core.observability import get_logger
from app.core.rbac import STAGE_ROLES, Permission

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


PLANNING_INSTRUCTION = """You are the Planning agent in Quipu's SDLC pipeline.
Given a feature request, work through it in this order and fill every field:

1. feature_summary — restate the feature in one or two sentences.
2. architecture_notes — how this fits the existing system at a high level.
3. affected_components — each system/module touched, and why.
4. tasks — ordered, concrete engineering tasks, each small enough for a
   single Architecture/Coding pass. Reference other tasks by id in depends_on.
5. dependencies — external dependencies outside this task list (other teams,
   services, infra, approvals).
6. acceptance_criteria — concrete, testable conditions for "done".
7. risks — what could go wrong, each with a mitigation.

Return only the structured plan."""


def _rbac_gate(callback_context: CallbackContext) -> None:
    STAGE_ROLES["planning"].requires(Permission.READ_KNOWLEDGE_BASE)


def _track_usage(callback_context: CallbackContext, llm_response: LlmResponse) -> None:
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


planning_agent = LlmAgent(
    name="planning",
    description="Breaks a detected feature into an ordered task plan.",
    model=settings.gemini_model,
    instruction=PLANNING_INSTRUCTION,
    output_schema=PlanOutput,
    output_key="plan",
    before_agent_callback=_rbac_gate,
    after_model_callback=_track_usage,
)
