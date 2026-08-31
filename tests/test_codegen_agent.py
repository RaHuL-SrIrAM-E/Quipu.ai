"""Tests for the Codegen Agent (Level 1.7).

No real Gemini/ADK model call, no real knowledge backend, no real Firestore.
Filesystem mutation tests use real temp directories (tmp_path) — the safety
boundary itself must be exercised against a real filesystem, not mocked away.
"""

import asyncio
import json
from pathlib import Path

import pytest
from google.genai import types
from pydantic import ValidationError

from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.status import AgentStatus
from app.agents.architecture import ArchitectureOutput
from app.agents.codegen import CodegenAgent, CodegenOutput, _codegen_llm_agent
from app.config import get_settings
from app.domain import AgentInput, Artifact, ArtifactType, KnowledgeItem, KnowledgeRequest, KnowledgeType, Ticket, WorkflowStatus
from app.persistence.memory import InMemoryAgentExecutionRepository
from app.tools.codegen_tools import write_file

VALID_ARCHITECTURE = ArchitectureOutput(
    design_summary="Add a ThemeProvider and a settings toggle.",
    components=[{"name": "ThemeProvider", "responsibility": "holds/applies theme state"}],
    task_designs=[
        {"task_id": "t1", "approach": "create ThemeProvider", "files": ["src/theme.py"]},
        {"task_id": "t2", "approach": "wire toggle", "files": ["src/settings.py"]},
    ],
    risks=[{"description": "theme flash on load", "mitigation": "inline script"}],
)

VALID_CODEGEN = {
    "summary": "Implemented ThemeProvider and settings toggle.",
    "modified_files": [],
    "created_files": ["src/theme.py", "src/settings.py"],
    "deleted_files": [],
    "changes": [
        {"path": "src/theme.py", "change_type": "created", "description": "theme provider"},
        {"path": "src/settings.py", "change_type": "created", "description": "settings toggle"},
    ],
    "implementation_notes": "",
    "unresolved_items": [],
    "tests_to_run": ["test_theme_provider"],
}


def make_architecture_artifact(workflow_id="wf-1", **overrides) -> Artifact:
    defaults = dict(
        artifact_type=ArtifactType.ARCHITECTURE,
        created_by="architecture_agent",
        payload=VALID_ARCHITECTURE.model_dump(mode="json"),
    )
    defaults.update(overrides)
    return Artifact(**defaults)


# ---- fakes ------------------------------------------------------------------


class FakeArtifactGateway:
    def __init__(self):
        self.saved: dict[tuple[str, str], Artifact] = {}

    def seed(self, workflow_id: str, artifact: Artifact) -> None:
        self.saved[(workflow_id, artifact.artifact_id)] = artifact

    async def get(self, workflow_id, artifact_id):
        return self.saved.get((workflow_id, artifact_id))

    async def save(self, workflow_id, artifact):
        self.saved[(workflow_id, artifact.artifact_id)] = artifact
        return artifact


class FakeToolGateway:
    async def execute(self, request):
        raise NotImplementedError


class FakeKnowledgeGateway:
    def __init__(self, items=None, raise_error: Exception | None = None):
        self._items = items or []
        self._raise_error = raise_error
        self.last_request: KnowledgeRequest | None = None

    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]:
        self.last_request = request
        if self._raise_error:
            raise self._raise_error
        return self._items


class _FakeEvent:
    def __init__(self, text):
        self.content = types.Content(role="model", parts=[types.Part(text=text)])

    def is_final_response(self):
        return True


class _FakeSession:
    id = "session-1"


class _FakeSessionService:
    async def create_session(self, **kwargs):
        return _FakeSession()


def make_fake_runner(write_paths: list[str] | None = None, final_text: str | None = None, raise_error: Exception | None = None):
    """write_paths: files this fake 'model turn' actually writes to disk
    (simulating write_file tool calls), so the agent's real-filesystem
    verification has something to detect."""

    async def _events(**kwargs):
        if raise_error:
            raise raise_error
        if write_paths:
            state = kwargs["new_message"]  # unused; writes happen via closure below
        if final_text is not None:
            yield _FakeEvent(final_text)

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()
            self._state = None

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


def make_agent_input(**overrides) -> AgentInput:
    defaults = dict(
        workflow_id="wf-1",
        agent_name="codegen_agent",
        ticket=Ticket(title="Add dark mode", description="Users want a dark theme toggle."),
        artifact_ids=["arch-1"],
    )
    defaults.update(overrides)
    return AgentInput(**defaults)


def make_context(workspace: Path, **overrides) -> AgentContext:
    gateway = overrides.pop("artifacts", None) or FakeArtifactGateway()
    defaults = dict(
        workflow_id="wf-1",
        execution_id="exec-1",
        knowledge=FakeKnowledgeGateway(),
        tools=FakeToolGateway(),
        artifacts=gateway,
        executions=InMemoryAgentExecutionRepository(),
        metadata={},
    )
    defaults.update(overrides)
    context = AgentContext(**defaults)
    return context


def make_agent_input_with_workspace(workspace: Path, **overrides) -> AgentInput:
    overrides.setdefault("context", {})
    overrides["context"] = {**overrides["context"], "workspace_path": str(workspace)}
    return make_agent_input(**overrides)


def make_context_with_architecture(architecture_artifact_id="arch-1", **overrides) -> AgentContext:
    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", make_architecture_artifact(artifact_id=architecture_artifact_id))
    workspace = overrides.pop("workspace")
    return make_context(workspace, artifacts=gateway, **overrides)


class _FakeToolContext:
    def __init__(self, state):
        self.state = state


# ---- Runtime ------------------------------------------------------------------


def test_codegen_agent_identity():
    agent = CodegenAgent()
    assert agent.identity.agent_id == "codegen_agent"
    assert agent.identity.name == "Codegen Agent"


def test_codegen_agent_expected_capabilities():
    agent = CodegenAgent()
    assert agent.capabilities == {
        AgentCapability.READ_TICKET,
        AgentCapability.READ_ARTIFACT,
        AgentCapability.QUERY_KNOWLEDGE,
        AgentCapability.READ_REPOSITORY,
        AgentCapability.WRITE_ARTIFACT,
        AgentCapability.WRITE_CODE,
    }
    forbidden = {AgentCapability.DEPLOY, AgentCapability.WRITE_JIRA, AgentCapability.RESOLVE_INCIDENT}
    assert agent.capabilities.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_lifecycle(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(final_text=json.dumps(VALID_CODEGEN)))

    agent = CodegenAgent()
    assert agent.status == AgentStatus.CREATED
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )
    assert agent.status == AgentStatus.COMPLETED
    assert output.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_failure_lifecycle(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(raise_error=RuntimeError("gemini down")))

    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )
    assert agent.status == AgentStatus.COMPLETED  # handled failure, not an uncaught exception
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "CODEGEN_LLM_FAILURE"


# ---- Timeout budget (codegen_llm_call_timeout_seconds, separate from the
# shared llm_call_timeout_seconds Planning/Architecture use) --------------


def make_slow_fake_runner(delay_seconds: float, final_text: str):
    """A fake ADK runner whose single turn takes real wall-clock time
    before yielding — the only way to genuinely exercise with_timeout's
    asyncio.wait_for bound rather than a canned instantaneous response."""

    async def _events(**kwargs):
        await asyncio.sleep(delay_seconds)
        yield _FakeEvent(final_text)

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


@pytest.mark.asyncio
async def test_codegen_timeout_uses_dedicated_setting_not_shared(monkeypatch, tmp_path: Path):
    """codegen_llm_call_timeout_seconds governs Codegen's timeout even when
    the shared llm_call_timeout_seconds is left large — proving Codegen
    reads its own setting, not the one Planning/Architecture use.

    Patches app.agents.codegen's own module-level `settings` object
    directly, NOT a fresh get_settings() call — other test modules call
    get_settings.cache_clear(), which would otherwise return a different
    Settings instance than the one codegen.py captured at import time and
    silently no-op this test's monkeypatch when run as part of the full
    suite (see tests/test_planning_agent.py for the identical issue)."""
    import app.agents.codegen as codegen_module

    monkeypatch.setattr(codegen_module.settings, "codegen_llm_call_timeout_seconds", 0.05)
    monkeypatch.setattr(codegen_module.settings, "llm_call_timeout_seconds", 60.0)
    monkeypatch.setattr(
        "app.agents.codegen.InMemoryRunner", make_slow_fake_runner(delay_seconds=0.5, final_text=json.dumps(VALID_CODEGEN))
    )

    agent = CodegenAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path))

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "CODEGEN_LLM_FAILURE"
    assert "did not complete within 0.05" in output.errors[0].message


@pytest.mark.asyncio
async def test_codegen_ignores_shared_llm_call_timeout(monkeypatch, tmp_path: Path):
    """The inverse: shrinking the SHARED setting alone must not affect
    Codegen — its own dedicated setting is what's actually consulted."""
    import app.agents.codegen as codegen_module

    monkeypatch.setattr(codegen_module.settings, "llm_call_timeout_seconds", 0.05)
    monkeypatch.setattr(codegen_module.settings, "codegen_llm_call_timeout_seconds", 5.0)
    monkeypatch.setattr(
        "app.agents.codegen.InMemoryRunner", make_slow_fake_runner(delay_seconds=0.2, final_text=json.dumps(VALID_CODEGEN))
    )

    agent = CodegenAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path))

    assert output.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_codegen_dedicated_timeout_defaults_to_120_seconds():
    assert get_settings().codegen_llm_call_timeout_seconds == 120.0


# ---- Demo mode (Settings.codegen_demo_mode) --------------------------------
#
# Patches app.agents.codegen's own module-level `settings` object directly,
# NOT a fresh get_settings() call — see tests/test_planning_agent.py's
# identical note for why (other test modules call get_settings.cache_clear(),
# which would otherwise return a different Settings instance than the one
# codegen.py captured at import time).


def test_codegen_demo_mode_defaults_to_false():
    assert get_settings().codegen_demo_mode is False


@pytest.mark.asyncio
async def test_codegen_demo_mode_false_still_uses_real_llm_path(monkeypatch, tmp_path: Path):
    """A. CODEGEN_DEMO_MODE=false -> the real Codegen LLM path is still used."""
    import app.agents.codegen as codegen_module

    monkeypatch.setattr(codegen_module.settings, "codegen_demo_mode", False)
    runner_calls = []
    real_runner = make_fake_runner(final_text=json.dumps(VALID_CODEGEN))

    def _spy_runner(agent, app_name):
        runner_calls.append(1)
        return real_runner(agent, app_name)

    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", _spy_runner)

    agent = CodegenAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path))

    assert runner_calls == [1]
    assert output.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_codegen_demo_mode_true_never_calls_real_llm_path(monkeypatch, tmp_path: Path):
    """B. CODEGEN_DEMO_MODE=true -> the real Codegen LLM path is not called."""
    import app.agents.codegen as codegen_module

    monkeypatch.setattr(codegen_module.settings, "codegen_demo_mode", True)

    def _boom(agent, app_name):
        raise AssertionError("InMemoryRunner must never be constructed in demo mode")

    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", _boom)

    agent = CodegenAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path))

    assert output.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_codegen_demo_mode_consumes_architecture_input(monkeypatch, tmp_path: Path):
    """E. Demo Codegen consumes the actual Architecture artifact — the
    files it writes come from architecture.task_designs, not a hardcoded
    list, and change when the architecture does."""
    import app.agents.codegen as codegen_module

    monkeypatch.setattr(codegen_module.settings, "codegen_demo_mode", True)
    monkeypatch.setattr(
        "app.agents.codegen.InMemoryRunner", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be constructed"))
    )

    custom_architecture = ArchitectureOutput(
        design_summary="Enhanced Reporting Export Capabilities.",
        components=[{"name": "ReportExporter", "responsibility": "exports report data as CSV"}],
        task_designs=[{"task_id": "t1", "approach": "add CSV export endpoint", "files": ["src/reporting/export.py"]}],
        risks=[],
    )
    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", make_architecture_artifact(artifact_id="arch-1", payload=custom_architecture.model_dump(mode="json")))
    context = make_context(tmp_path, artifacts=gateway)

    output = await CodegenAgent().execute(make_agent_input_with_workspace(tmp_path), context)

    assert output.status == WorkflowStatus.COMPLETED
    written = tmp_path / "src" / "reporting" / "export.py"
    assert written.is_file()
    assert "add CSV export endpoint" in written.read_text()
    codegen_payload = output.artifacts[0].payload
    assert codegen_payload["created_files"] == ["src/reporting/export.py"]


@pytest.mark.asyncio
async def test_codegen_demo_mode_creates_normal_code_change_artifact(monkeypatch, tmp_path: Path):
    """G/I. Demo Codegen creates a normal CODE_CHANGE artifact — same
    artifact_type, same payload schema (CodegenOutput) as a real run,
    plus the additive execution_mode marker."""
    import app.agents.codegen as codegen_module

    monkeypatch.setattr(codegen_module.settings, "codegen_demo_mode", True)

    output = await CodegenAgent().execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )

    assert output.status == WorkflowStatus.COMPLETED
    artifact = output.artifacts[0]
    assert artifact.artifact_type == ArtifactType.CODE_CHANGE
    # Payload still validates as the exact same CodegenOutput schema real
    # Codegen produces — demo mode adds a key, never changes the shape.
    CodegenOutput.model_validate(artifact.payload)
    assert artifact.payload["execution_mode"] == "demo"


@pytest.mark.asyncio
async def test_codegen_real_mode_payload_never_has_execution_mode_key(monkeypatch, tmp_path: Path):
    """Real-mode payload is byte-identical to before this setting
    existed — no execution_mode key ever appears when demo mode is off."""
    import app.agents.codegen as codegen_module

    monkeypatch.setattr(codegen_module.settings, "codegen_demo_mode", False)
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(final_text=json.dumps(VALID_CODEGEN)))

    output = await CodegenAgent().execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )

    assert "execution_mode" not in output.artifacts[0].payload


# ---- Input ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_architecture_artifact_loaded(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(final_text=json.dumps(VALID_CODEGEN)))

    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )
    assert output.artifacts[0].parent_artifact_ids == ["arch-1"]


@pytest.mark.asyncio
async def test_wrong_artifact_type_rejected(tmp_path: Path):
    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", Artifact(artifact_id="arch-1", artifact_type=ArtifactType.PLAN, created_by="x", payload={}))

    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context(tmp_path, artifacts=gateway)
    )
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "ARCHITECTURE_ARTIFACT_WRONG_TYPE"


@pytest.mark.asyncio
async def test_missing_artifact_rejected(tmp_path: Path):
    agent = CodegenAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context(tmp_path))
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "ARCHITECTURE_ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_invalid_architecture_output_rejected(tmp_path: Path):
    gateway = FakeArtifactGateway()
    gateway.seed(
        "wf-1",
        Artifact(artifact_id="arch-1", artifact_type=ArtifactType.ARCHITECTURE, created_by="x", payload={"task_designs": []}),
    )
    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context(tmp_path, artifacts=gateway)
    )
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "ARCHITECTURE_OUTPUT_INVALID"


# ---- Knowledge ------------------------------------------------------------


def test_knowledge_tool_available():
    tool_names = {t.__name__ for t in _codegen_llm_agent.tools if callable(t)}
    assert "query_enterprise_knowledge" in tool_names


@pytest.mark.asyncio
async def test_codegen_retrieval_profile_used():
    from app.knowledge.policies import get_retrieval_policy
    from app.tools.knowledge_tools import query_enterprise_knowledge

    gateway = FakeKnowledgeGateway(
        items=[
            KnowledgeItem(
                document_id="doc-1",
                title="Naming conventions",
                content="use snake_case for python modules",
                knowledge_type=KnowledgeType.CODING_STANDARD,
                source="wiki",
            )
        ]
    )
    tool_context = _FakeToolContext({"_knowledge_gateway": gateway, "workflow_id": "wf-1", "_agent_name": "codegen_agent"})
    result = await query_enterprise_knowledge("naming conventions", "coding_standard", tool_context)

    assert len(result) == 1
    policy = get_retrieval_policy("codegen_agent")
    assert gateway.last_request.agent_name == "codegen_agent"
    assert gateway.last_request.knowledge_type in policy.allowed_knowledge_types


@pytest.mark.asyncio
async def test_knowledge_provenance_preserved_for_codegen():
    from app.tools.knowledge_tools import query_enterprise_knowledge

    item = KnowledgeItem(
        document_id="doc-9",
        title="Secure coding",
        content="never log secrets",
        knowledge_type=KnowledgeType.SECURITY_POLICY,
        source="policy://secure-coding",
        relevance_score=0.8,
    )
    gateway = FakeKnowledgeGateway(items=[item])
    tool_context = _FakeToolContext({"_knowledge_gateway": gateway, "workflow_id": "wf-1", "_agent_name": "codegen_agent"})
    result = await query_enterprise_knowledge("secure coding", "security_policy", tool_context)

    assert result[0]["document_id"] == "doc-9"
    assert result[0]["source"] == "policy://secure-coding"


# ---- Repository -------------------------------------------------------------


def test_repository_inspection_works():
    tool_names = {t.__name__ for t in _codegen_llm_agent.tools if callable(t)}
    assert {"search_files", "read_file", "search_code", "get_project_structure", "get_dependencies"} <= tool_names


def test_existing_repository_tools_reused(tmp_path: Path):
    from app.tools.repo_tools import read_file

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "theme.py").write_text("class ThemeProvider: pass")

    tool_context = _FakeToolContext({"workspace_path": str(tmp_path)})
    assert read_file("src/theme.py", tool_context) == "class ThemeProvider: pass"


# ---- Mutation security --------------------------------------------------------


def test_write_code_capability_required(tmp_path: Path):
    tool_context = _FakeToolContext({"_capabilities": set(), "_allowed_paths": ["src/theme.py"], "workspace_path": str(tmp_path)})
    result = write_file("src/theme.py", "content", tool_context)
    assert result["success"] is False
    assert "WRITE_CODE" in result["error"]
    assert not (tmp_path / "src" / "theme.py").exists()


def test_allowed_file_write_succeeds(tmp_path: Path):
    tool_context = _FakeToolContext(
        {"_capabilities": {AgentCapability.WRITE_CODE}, "_allowed_paths": ["src/theme.py"], "workspace_path": str(tmp_path)}
    )
    result = write_file("src/theme.py", "class ThemeProvider: pass", tool_context)
    assert result["success"] is True
    assert (tmp_path / "src" / "theme.py").read_text() == "class ThemeProvider: pass"


def test_disallowed_file_write_rejected(tmp_path: Path):
    tool_context = _FakeToolContext(
        {"_capabilities": {AgentCapability.WRITE_CODE}, "_allowed_paths": ["src/theme.py"], "workspace_path": str(tmp_path)}
    )
    result = write_file("src/unrelated.py", "malicious", tool_context)
    assert result["success"] is False
    assert "outside the architecture-approved scope" in result["error"]
    assert not (tmp_path / "src" / "unrelated.py").exists()


def test_path_traversal_rejected(tmp_path: Path):
    outside = tmp_path.parent / "traversal_target.txt"
    tool_context = _FakeToolContext(
        {
            "_capabilities": {AgentCapability.WRITE_CODE},
            "_allowed_paths": ["../traversal_target.txt"],
            "workspace_path": str(tmp_path),
        }
    )
    result = write_file("../traversal_target.txt", "pwned", tool_context)
    assert result["success"] is False
    assert not outside.exists()


def test_absolute_path_rejected(tmp_path: Path):
    tool_context = _FakeToolContext(
        {"_capabilities": {AgentCapability.WRITE_CODE}, "_allowed_paths": ["/etc/passwd"], "workspace_path": str(tmp_path)}
    )
    result = write_file("/etc/passwd", "pwned", tool_context)
    assert result["success"] is False
    assert "absolute paths" in result["error"]


def test_repository_root_escape_rejected(tmp_path: Path):
    tool_context = _FakeToolContext(
        {
            "_capabilities": {AgentCapability.WRITE_CODE},
            "_allowed_paths": ["../../secret.txt"],
            "workspace_path": str(tmp_path),
        }
    )
    result = write_file("../../secret.txt", "pwned", tool_context)
    assert result["success"] is False


@pytest.mark.asyncio
async def test_scope_violation_detected_end_to_end(monkeypatch, tmp_path: Path):
    """Even if a write somehow lands outside allowed_paths (bypassing the
    tool's own gate), CodegenAgent's post-hoc filesystem check must catch it."""
    (tmp_path / "src").mkdir()

    async def _write_out_of_scope(**kwargs):
        (tmp_path / "src" / "unrelated.py").write_text("sneaky")  # simulate an out-of-scope write happening
        return
        yield  # pragma: no cover

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            async def _events():
                (tmp_path / "src" / "unrelated.py").write_text("sneaky")
                yield _FakeEvent(json.dumps(VALID_CODEGEN))

            return _events()

    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", _FakeRunner)

    # allowed_paths derived from VALID_ARCHITECTURE does not include src/unrelated.py
    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "CODEGEN_SCOPE_VIOLATION"


# ---- Output -----------------------------------------------------------------


def test_codegen_output_validates():
    output = CodegenOutput(**VALID_CODEGEN)
    assert output.summary
    with pytest.raises(ValidationError):
        CodegenOutput(summary="")
    with pytest.raises(ValidationError):
        CodegenOutput(summary="x", changes=[{"path": "a", "change_type": "bogus"}])


@pytest.mark.asyncio
async def test_actual_modified_files_captured_not_llm_self_report(monkeypatch, tmp_path: Path):
    """The LLM claims it created two files; only actually writing them makes
    them show up in the final CodegenOutput."""
    claimed_but_not_written = json.dumps(VALID_CODEGEN)  # claims src/theme.py and src/settings.py created

    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(final_text=claimed_but_not_written))

    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )

    assert output.status == WorkflowStatus.COMPLETED
    artifact_payload = output.artifacts[0].payload
    # nothing was actually written to disk by this fake runner, so ground truth is empty
    assert artifact_payload["created_files"] == []
    assert artifact_payload["modified_files"] == []


@pytest.mark.asyncio
async def test_real_write_reflected_in_artifact(monkeypatch, tmp_path: Path):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            async def _events():
                (tmp_path / "src").mkdir(exist_ok=True)
                (tmp_path / "src" / "theme.py").write_text("class ThemeProvider: pass")
                yield _FakeEvent(json.dumps(VALID_CODEGEN))

            return _events()

    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", _FakeRunner)

    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )

    assert output.status == WorkflowStatus.COMPLETED
    assert output.artifacts[0].payload["created_files"] == ["src/theme.py"]


@pytest.mark.asyncio
async def test_code_artifact_created(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(final_text=json.dumps(VALID_CODEGEN)))

    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context_with_architecture(workspace=tmp_path)
    )
    assert output.artifacts[0].artifact_type == ArtifactType.CODE_CHANGE


def test_code_artifact_parent_is_architecture_artifact():
    artifact = Artifact(
        artifact_type=ArtifactType.CODE_CHANGE,
        created_by="codegen_agent",
        parent_artifact_ids=["arch-1"],
        payload=VALID_CODEGEN,
    )
    assert artifact.parent_artifact_ids == ["arch-1"]


@pytest.mark.asyncio
async def test_artifact_gateway_used_for_persistence(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(final_text=json.dumps(VALID_CODEGEN)))

    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", make_architecture_artifact(artifact_id="arch-1"))
    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path), make_context(tmp_path, artifacts=gateway)
    )
    artifact_id = output.artifacts[0].artifact_id
    assert gateway.saved[("wf-1", artifact_id)] is not None


# ---- ADK --------------------------------------------------------------------


def test_internal_llm_agent_uses_gemini():
    from app.config import get_settings

    assert _codegen_llm_agent.model == get_settings().gemini_model


def test_internal_llm_agent_uses_structured_codegen_output():
    assert _codegen_llm_agent.output_schema is CodegenOutput


def test_adk_tool_boundary_enforces_capability_checks():
    from app.agent_runtime.capabilities import CapabilityError
    from app.agents.planning import _tool_capability_gate

    class _FakeTool:
        name = "write_file"

    class _FakeToolCtx:
        state = {"_capabilities": set()}  # WRITE_CODE not granted

    with pytest.raises(CapabilityError):
        _tool_capability_gate(_FakeTool(), {}, _FakeToolCtx())


# ---- Execution ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_execution_records_input_output_artifacts(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(final_text=json.dumps(VALID_CODEGEN)))

    executions = InMemoryAgentExecutionRepository()
    agent = CodegenAgent()
    output = await agent.execute(
        make_agent_input_with_workspace(tmp_path, execution_id="exec-99"),
        make_context_with_architecture(workspace=tmp_path, executions=executions, execution_id="exec-99"),
    )

    execution = await executions.get("wf-1", "exec-99")
    assert execution.status == WorkflowStatus.COMPLETED
    assert execution.output_artifact_ids == [output.artifacts[0].artifact_id]


@pytest.mark.asyncio
async def test_metrics_captured(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_fake_runner(final_text=json.dumps(VALID_CODEGEN)))

    agent = CodegenAgent()
    agent_input = make_agent_input_with_workspace(tmp_path, execution_id="exec-metrics")
    output = await agent.execute(agent_input, make_context_with_architecture(workspace=tmp_path))
    assert output.metrics is not None
    assert output.metrics.execution_id == "exec-metrics"


# ---- Regression ---------------------------------------------------------------


def test_existing_app_and_legacy_pipeline_still_import():
    from app.main import app  # noqa: F401
    from app.orchestrator.pipeline import quipu_pipeline

    assert [a.name for a in quipu_pipeline.sub_agents] == ["feature_detection", "planning", "architecture"]
