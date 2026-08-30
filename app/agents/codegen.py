"""Codegen agent — the first Quipu-native agent with no legacy ADK
predecessor (no coexistence pattern needed here, unlike planning.py/
architecture.py). Follows the same QuipuAgent + internal-ADK-adapter shape.

Architecture decides WHAT/HOW/WHICH files; Codegen decides HOW to actually
implement that design in code, constrained to exactly the files Architecture
named. It never redesigns the architecture and never calls another agent —
inter-agent communication is artifacts + the future orchestrator.

See docs/architecture/codegen_agent.md for the full design, especially the
repository-mutation safety boundary (§5-9 there).
"""

import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.runners import InMemoryRunner
from google.genai import types

import json

from app.agent_runtime.base import QuipuAgent
from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.identity import AgentIdentity
from app.agents.architecture import ArchitectureOutput
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
from app.tools.codegen_tools import CODEGEN_TOOLS
from app.tools.knowledge_tools import KNOWLEDGE_TOOLS
from app.tools.repo_tools import REPO_TOOLS

logger = get_logger("quipu.agent.codegen")
settings = get_settings()


class FileChange(BaseModel):
    path: str
    change_type: str
    description: str = ""

    _validate_path = field_validator("path")(_non_empty)

    @field_validator("change_type")
    @classmethod
    def _valid_change_type(cls, value: str) -> str:
        if value not in {"created", "modified", "deleted"}:
            raise ValueError("change_type must be one of: created, modified, deleted")
        return value


class CodegenOutput(BaseModel):
    summary: str
    modified_files: list[str] = Field(default_factory=list)
    created_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    changes: list[FileChange] = Field(default_factory=list)
    implementation_notes: str = ""
    unresolved_items: list[str] = Field(default_factory=list)
    tests_to_run: list[str] = Field(default_factory=list)

    _validate_summary = field_validator("summary")(_non_empty)


def _build_instruction(context: ReadonlyContext) -> str:
    architecture = context.state.get("architecture")
    architecture_json = json.dumps(architecture, indent=2) if architecture else "(no architecture found in session state)"
    allowed_paths = context.state.get("_allowed_paths", [])
    allowed_list = "\n".join(f"- {p}" for p in allowed_paths) or "(none — nothing in scope for this task)"

    knowledge_note = ""
    if context.state.get("_knowledge_gateway") is not None:
        knowledge_note = (
            "\n\nYou also have query_enterprise_knowledge — coding standards, "
            "approved libraries, naming conventions, secure coding requirements, "
            "framework patterns, testing conventions. Use it when the "
            "implementation needs enterprise-specific guidance; don't call it "
            "for every file."
        )

    return f"""You are Quipu's Codegen Agent.

The Architecture stage has already decided WHAT should change, HOW, and
WHICH files — it is authoritative. Implement that design; do not redesign
it, and do not second-guess its component/task boundaries.

Architecture (authoritative):
{architecture_json}

You have tools to inspect the current implementation: get_project_structure,
search_files, search_code, read_file, get_dependencies. Inspect the real
code before changing it — match existing patterns and conventions, don't
guess.{knowledge_note}

You may only write to these files (the architecture-approved scope for this
work):
{allowed_list}

Use write_file(path, content) to make changes — no shell commands are
available to you, and none will be. write_file refuses any path outside the
list above; if you discover a genuinely necessary file outside this scope,
report it in unresolved_items instead of trying to work around the refusal.

Produce actual, working code — not a sketch. Then return, structured:

1. summary — what you implemented, briefly.
2. modified_files / created_files / deleted_files — what you changed (the
   application verifies this against the real filesystem afterward, so it's
   fine if this is approximate).
3. changes — per-file detail: path, change_type, description.
4. implementation_notes — anything a reviewer should know.
5. unresolved_items — anything you couldn't do within scope.
6. tests_to_run — what should be tested, for the Testing stage later.

Return only the structured result."""


_codegen_llm_agent = LlmAgent(
    name="codegen",
    description="Implements an approved architecture as actual code, within an explicit file scope.",
    model=settings.gemini_model,
    instruction=_build_instruction,
    output_schema=CodegenOutput,
    output_key="codegen",
    tools=REPO_TOOLS + KNOWLEDGE_TOOLS + CODEGEN_TOOLS,
    before_tool_callback=_tool_capability_gate,
    after_model_callback=_track_usage_metrics,
)


def _snapshot(root: Path) -> dict[str, float]:
    """Scans the WHOLE workspace tree, not just allowed_paths — the point of
    this snapshot is to independently detect any write, including one that
    would land outside the approved scope, so scope-violation detection
    (see _perform's out_of_scope check) is real defense-in-depth rather than
    only ever looking where a violation could never be found.
    """
    snapshot: dict[str, float] = {}
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        snapshot[str(path.relative_to(root))] = path.stat().st_mtime
    return snapshot


def _diff_snapshot(before: dict[str, float], after: dict[str, float]) -> tuple[list[str], list[str], list[str]]:
    created = [path for path in after if path not in before]
    deleted = [path for path in before if path not in after]
    modified = [path for path in after if path in before and after[path] != before[path]]
    return sorted(created), sorted(modified), sorted(deleted)


class CodegenAgent(QuipuAgent):
    """Quipu-native Codegen Agent. Consumes the ArchitectureArtifact (via
    ArtifactGateway), inspects the real repo, optionally consults enterprise
    knowledge, implements exactly the architecture-approved files through the
    capability-gated, scope-checked write_file tool, verifies the actual
    filesystem change (never trusting the model's self-report), and persists
    the result as a CodeArtifact. Never calls another agent.
    """

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id="codegen_agent",
            name="Codegen Agent",
            version="1.0.0",
            description="Implements an approved architecture as code, within an explicit file scope.",
        )

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {
            AgentCapability.READ_TICKET,
            AgentCapability.READ_ARTIFACT,
            AgentCapability.QUERY_KNOWLEDGE,
            AgentCapability.READ_REPOSITORY,
            AgentCapability.WRITE_ARTIFACT,
            AgentCapability.WRITE_CODE,
        }

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        self.require_capability(AgentCapability.READ_TICKET)
        self.require_capability(AgentCapability.READ_ARTIFACT)
        self.require_capability(AgentCapability.READ_REPOSITORY)
        self.require_capability(AgentCapability.WRITE_CODE)

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

        # --- Consume the Architecture result through the artifact abstraction.
        if not agent_input.artifact_ids:
            return await _fail(
                "ARCHITECTURE_ARTIFACT_MISSING",
                "AgentInput.artifact_ids is empty; no architecture artifact reference given",
                ErrorCategory.VALIDATION,
            )

        architecture_artifact_id = agent_input.artifact_ids[0]
        architecture_artifact = await context.artifacts.get(agent_input.workflow_id, architecture_artifact_id)
        if architecture_artifact is None:
            return await _fail(
                "ARCHITECTURE_ARTIFACT_MISSING",
                f"no artifact '{architecture_artifact_id}' found for workflow '{agent_input.workflow_id}'",
                ErrorCategory.VALIDATION,
            )
        if architecture_artifact.artifact_type != ArtifactType.ARCHITECTURE:
            return await _fail(
                "ARCHITECTURE_ARTIFACT_WRONG_TYPE",
                f"artifact '{architecture_artifact_id}' has type '{architecture_artifact.artifact_type}', "
                f"expected '{ArtifactType.ARCHITECTURE}'",
                ErrorCategory.VALIDATION,
            )
        try:
            architecture = ArchitectureOutput.model_validate(architecture_artifact.payload)
        except ValidationError as exc:
            return await _fail("ARCHITECTURE_OUTPUT_INVALID", str(exc), ErrorCategory.VALIDATION)

        # --- Allowed-file scope, derived from the architecture — not invented,
        # not expanded, not editable by the model.
        allowed_paths: set[str] = set()
        for task_design in architecture.task_designs:
            allowed_paths.update(task_design.files)

        workspace_path = agent_input.context.get("workspace_path")
        if not workspace_path:
            return await _fail(
                "CODEGEN_WORKSPACE_MISSING", "no repo checked out for this run (missing workspace_path)", ErrorCategory.INTERNAL
            )
        root = Path(workspace_path)

        before_snapshot = _snapshot(root)

        session_state: dict = {
            "architecture": architecture.model_dump(mode="json"),
            "workspace_path": workspace_path,
            "workflow_id": agent_input.workflow_id,
            "_agent_name": self.identity.agent_id,
            "_capabilities": self.capabilities,
            "_allowed_paths": sorted(allowed_paths),
            "_metrics": metrics,
        }
        if AgentCapability.QUERY_KNOWLEDGE in self.capabilities:
            session_state["_knowledge_gateway"] = context.knowledge

        runner = InMemoryRunner(agent=_codegen_llm_agent, app_name="quipu")
        session = await runner.session_service.create_session(
            app_name="quipu", user_id=agent_input.workflow_id, state=session_state
        )
        message = types.Content(role="user", parts=[types.Part(text="Begin implementation.")])

        final_text = ""
        try:
            async def _consume_llm_response() -> None:
                nonlocal final_text
                async for event in runner.run_async(user_id=agent_input.workflow_id, session_id=session.id, new_message=message):
                    if event.is_final_response() and event.content and event.content.parts:
                        final_text = event.content.parts[0].text

            await with_timeout(_consume_llm_response(), settings.codegen_llm_call_timeout_seconds, operation="codegen_agent_llm_call")
        except Exception as exc:  # Gemini/ADK/tool failure — never fabricate a change.
            logger.exception("codegen agent LLM execution failed")
            return await _fail("CODEGEN_LLM_FAILURE", str(exc), ErrorCategory.LLM_FAILURE)

        if not final_text.strip():
            return await _fail("CODEGEN_EMPTY_RESPONSE", "model returned an empty response", ErrorCategory.LLM_FAILURE)

        try:
            codegen_output = CodegenOutput.model_validate_json(final_text)
        except ValidationError as exc:
            return await _fail("CODEGEN_VALIDATION_FAILED", str(exc), ErrorCategory.VALIDATION)

        # --- Never trust the model's self-report: verify the real filesystem.
        after_snapshot = _snapshot(root)
        actual_created, actual_modified, actual_deleted = _diff_snapshot(before_snapshot, after_snapshot)

        actual_touched = set(actual_created) | set(actual_modified) | set(actual_deleted)
        out_of_scope = actual_touched - allowed_paths
        if out_of_scope:
            # Should be structurally impossible (write_file itself refuses
            # out-of-scope paths) — this is defense-in-depth, not the primary
            # gate. See docs/architecture/codegen_agent.md "Scope validation".
            return await _fail(
                "CODEGEN_SCOPE_VIOLATION", f"modified files outside approved scope: {sorted(out_of_scope)}", ErrorCategory.VALIDATION
            )

        codegen_output = codegen_output.model_copy(
            update={"created_files": actual_created, "modified_files": actual_modified, "deleted_files": actual_deleted}
        )

        self.require_capability(AgentCapability.WRITE_ARTIFACT)
        artifact = Artifact(
            artifact_id=str(uuid.uuid4()),
            artifact_type=ArtifactType.CODE_CHANGE,
            created_by=self.identity.agent_id,
            parent_artifact_ids=[architecture_artifact_id],
            payload=codegen_output.model_dump(mode="json"),
        )
        try:
            await context.artifacts.save(agent_input.workflow_id, artifact)
        except Exception as exc:
            logger.exception("codegen artifact persistence failed")
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
            messages=[codegen_output.summary],
            metrics=metrics,
        )
