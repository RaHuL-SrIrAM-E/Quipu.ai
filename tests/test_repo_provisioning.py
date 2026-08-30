"""Tests for the workspace-provisioning layer:

    FeatureReview -> start_workflow_from_review() -> WorkflowState
        -> OrchestrationService._ensure_workspace() -> checkout
        -> workflow.metadata["workspace_path"] -> AgentInput.context

Covers repository-config resolution, workspace reuse/re-clone, workflow
isolation, credential handling (app.core.repo.clone_repo), and cleanup on
terminal workflow states. No real git/network call anywhere here —
app.core.repo.clone_repo is monkeypatched throughout except in the small
`_auth_env`/subprocess-argument tests, which mock `subprocess.run` instead
of hitting a real repository.
"""

import base64
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.repo import RepoCloneError, _auth_env, clone_repo
from app.demo.fakes import FakeKnowledgeGateway, FakeToolGateway
from app.domain import Ticket, WorkflowStage, WorkflowState, WorkflowStatus
from app.orchestration.errors import WorkspaceProvisioningError
from app.orchestration.registry_setup import build_default_registry
from app.orchestration.service import OrchestrationService
from app.persistence.memory import (
    InMemoryAgentExecutionRepository,
    InMemoryArtifactRepository,
    InMemoryDecisionRepository,
    InMemoryWorkflowRepository,
)


def make_service(workflow_repo=None) -> OrchestrationService:
    return OrchestrationService(
        workflow_repo=workflow_repo or InMemoryWorkflowRepository(),
        artifact_repo=InMemoryArtifactRepository(),
        execution_repo=InMemoryAgentExecutionRepository(),
        decision_repo=InMemoryDecisionRepository(),
        registry=build_default_registry(),
        knowledge_gateway=FakeKnowledgeGateway(),
        tool_gateway=FakeToolGateway(),
    )


def make_workflow(**ticket_metadata) -> WorkflowState:
    return WorkflowState(
        ticket=Ticket(title="t", description="d", metadata=ticket_metadata),
        current_stage=WorkflowStage.PLANNING,
    )


# ---------------------------------------------------------------------------
# A. First execution — no workspace_path yet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_execution_clones_and_persists_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "default_repo_url", "https://example.invalid/repo.git")
    monkeypatch.setattr(get_settings(), "default_repo_ref", None)
    calls = []

    def _fake_clone_repo(repo_url, run_id, ref=None):
        calls.append((repo_url, run_id, ref))
        dest = tmp_path / run_id
        dest.mkdir(parents=True)
        return dest

    monkeypatch.setattr("app.orchestration.service.clone_repo", _fake_clone_repo)

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow()
    await workflow_repo.create(workflow)

    path = await service._ensure_workspace(workflow)

    assert calls == [("https://example.invalid/repo.git", workflow.workflow_id, None)]
    assert path == str(tmp_path / workflow.workflow_id)
    stored = await workflow_repo.get(workflow.workflow_id)
    assert stored.metadata["workspace_path"] == path
    assert stored.version == workflow.version + 1


@pytest.mark.asyncio
async def test_ticket_metadata_ref_override_passed_to_clone(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "default_repo_url", "https://example.invalid/repo.git")
    captured = {}

    def _fake_clone_repo(repo_url, run_id, ref=None):
        captured["ref"] = ref
        dest = tmp_path / run_id
        dest.mkdir(parents=True)
        return dest

    monkeypatch.setattr("app.orchestration.service.clone_repo", _fake_clone_repo)

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow(repo_ref="feature-branch")
    await workflow_repo.create(workflow)

    await service._ensure_workspace(workflow)

    assert captured["ref"] == "feature-branch"


# ---------------------------------------------------------------------------
# B. Reuse — workspace_path already exists and is valid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reuses_existing_valid_workspace_without_cloning(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("app.orchestration.service.clone_repo", lambda *a, **k: calls.append(1) or tmp_path)

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow()
    workflow = workflow.model_copy(update={"metadata": {"workspace_path": str(tmp_path)}})
    await workflow_repo.create(workflow)

    path = await service._ensure_workspace(workflow)

    assert path == str(tmp_path)
    assert calls == []  # clone_repo never called
    stored = await workflow_repo.get(workflow.workflow_id)
    assert stored.version == workflow.version  # no write happened either


@pytest.mark.asyncio
async def test_reuse_requires_no_repository_configuration(monkeypatch, tmp_path):
    """A workflow that already has a valid workspace never needs
    default_repo_url/Ticket.metadata['repo_url'] at all — matches how
    tests/demo scenarios already hand start_workflow() a workspace_path
    directly without ever configuring a repository."""
    assert get_settings().default_repo_url is None  # nothing configured

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow()
    workflow = workflow.model_copy(update={"metadata": {"workspace_path": str(tmp_path)}})
    await workflow_repo.create(workflow)

    path = await service._ensure_workspace(workflow)
    assert path == str(tmp_path)


# ---------------------------------------------------------------------------
# C. Stale path — workspace_path set but directory no longer exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_workspace_path_triggers_reclone(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "default_repo_url", "https://example.invalid/repo.git")
    calls = []

    def _fake_clone_repo(repo_url, run_id, ref=None):
        calls.append(run_id)
        dest = tmp_path / "fresh" / run_id
        dest.mkdir(parents=True)
        return dest

    monkeypatch.setattr("app.orchestration.service.clone_repo", _fake_clone_repo)

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    stale_path = str(tmp_path / "does-not-exist-anymore")
    workflow = make_workflow()
    workflow = workflow.model_copy(update={"metadata": {"workspace_path": stale_path}})
    await workflow_repo.create(workflow)

    new_path = await service._ensure_workspace(workflow)

    assert calls == [workflow.workflow_id]
    assert new_path != stale_path
    stored = await workflow_repo.get(workflow.workflow_id)
    assert stored.metadata["workspace_path"] == new_path


# ---------------------------------------------------------------------------
# D. Missing repository configuration — deterministic, visible failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_repository_configuration_raises():
    assert get_settings().default_repo_url is None
    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow()
    await workflow_repo.create(workflow)

    with pytest.raises(WorkspaceProvisioningError, match="no repository configured"):
        await service._ensure_workspace(workflow)

    # No partial state persisted.
    stored = await workflow_repo.get(workflow.workflow_id)
    assert "workspace_path" not in stored.metadata


@pytest.mark.asyncio
async def test_missing_repository_configuration_fails_workflow_without_invoking_planning(monkeypatch):
    """execute_next_step must never let Planning start when no workspace
    can be provisioned — the whole point of this layer."""
    assert get_settings().default_repo_url is None
    invoked = []
    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)

    real_registry_get = service._registry.get

    def _spy_get(agent_id):
        invoked.append(agent_id)
        return real_registry_get(agent_id)

    monkeypatch.setattr(service._registry, "get", _spy_get)

    workflow = make_workflow()
    await workflow_repo.create(workflow)

    result = await service.execute_next_step(workflow.workflow_id)

    assert result.status == WorkflowStatus.FAILED
    assert result.current_stage == WorkflowStage.PLANNING
    assert "no repository configured" in result.metadata["failure_reason"]
    # planning_agent was resolved from the registry (needed to know which
    # agent *would* run) but PlanningAgent.execute() itself was never
    # reached — assert indirectly via no PLAN artifact and FAILED status,
    # since QuipuAgent has no directly-observable "did not run" flag here.
    assert result.artifact_ids == []


# ---------------------------------------------------------------------------
# E. Workflow isolation — two workflows, two separate workspace paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workflows_get_isolated_workspaces(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "default_repo_url", "https://example.invalid/repo.git")

    def _fake_clone_repo(repo_url, run_id, ref=None):
        dest = tmp_path / run_id
        dest.mkdir(parents=True)
        return dest

    monkeypatch.setattr("app.orchestration.service.clone_repo", _fake_clone_repo)

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    wf1 = make_workflow()
    wf2 = make_workflow()
    await workflow_repo.create(wf1)
    await workflow_repo.create(wf2)

    path1 = await service._ensure_workspace(wf1)
    path2 = await service._ensure_workspace(wf2)

    assert path1 != path2
    assert wf1.workflow_id in path1
    assert wf2.workflow_id in path2


# ---------------------------------------------------------------------------
# F. Credentials — never persisted into metadata, files, or .git/config;
# never on the subprocess command line.
# ---------------------------------------------------------------------------


def test_auth_env_empty_without_token():
    assert _auth_env(None) == {}
    assert _auth_env("") == {}


def test_auth_env_never_puts_raw_token_in_header_value():
    env = _auth_env("super-secret-token")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    # The raw token must never appear verbatim — only base64-encoded inside
    # a Basic auth header value.
    assert "super-secret-token" not in env["GIT_CONFIG_VALUE_0"]
    decoded = base64.b64decode(env["GIT_CONFIG_VALUE_0"].split(" ")[-1]).decode()
    assert decoded == "x-access-token:super-secret-token"


@pytest.mark.asyncio
async def test_clone_repo_never_puts_token_in_argv_or_url(monkeypatch, tmp_path):
    """The command line git actually runs (visible to `ps`, and to any
    logging of `cmd`) must never contain the token, and neither must the
    repo_url passed to it."""
    monkeypatch.setattr(get_settings(), "workspace_root", str(tmp_path))
    monkeypatch.setattr(get_settings(), "git_access_token", "super-secret-token")

    captured = {}

    class _FakeResult:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, capture_output, text, timeout, env):
        captured["cmd"] = cmd
        captured["env"] = env
        Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        return _FakeResult()

    monkeypatch.setattr("app.core.repo.subprocess.run", _fake_run)

    clone_repo("https://github.com/example/private-repo.git", "run-1")

    assert "super-secret-token" not in " ".join(captured["cmd"])
    assert captured["cmd"][-2] == "https://github.com/example/private-repo.git"  # unmodified, no embedded credential
    # The credential travels only via subprocess-scoped env, never argv.
    assert "super-secret-token" not in captured["env"]["GIT_CONFIG_VALUE_0"]


@pytest.mark.asyncio
async def test_workspace_path_metadata_never_contains_the_token(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "default_repo_url", "https://example.invalid/repo.git")
    monkeypatch.setattr(get_settings(), "git_access_token", "super-secret-token")

    def _fake_clone_repo(repo_url, run_id, ref=None):
        dest = tmp_path / run_id
        dest.mkdir(parents=True)
        return dest

    monkeypatch.setattr("app.orchestration.service.clone_repo", _fake_clone_repo)

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow()
    await workflow_repo.create(workflow)

    await service._ensure_workspace(workflow)

    stored = await workflow_repo.get(workflow.workflow_id)
    assert "super-secret-token" not in str(stored.metadata)


def test_clone_repo_failure_message_never_leaks_token(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "workspace_root", str(tmp_path))
    monkeypatch.setattr(get_settings(), "git_access_token", "super-secret-token")

    class _FakeResult:
        returncode = 128
        stderr = "fatal: Authentication failed for 'https://github.com/example/private-repo.git/'"

    monkeypatch.setattr("app.core.repo.subprocess.run", lambda *a, **k: _FakeResult())

    with pytest.raises(RepoCloneError) as excinfo:
        clone_repo("https://github.com/example/private-repo.git", "run-2")

    assert "super-secret-token" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# G. Cleanup — terminal states reclaim the workspace; non-terminal states don't.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_invoked_on_failure(monkeypatch):
    calls = []
    monkeypatch.setattr("app.orchestration.service.cleanup_workspace", lambda workflow_id: calls.append(workflow_id))

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow()  # no repo configured -> Planning fails deterministically
    await workflow_repo.create(workflow)

    result = await service.execute_next_step(workflow.workflow_id)

    assert result.status == WorkflowStatus.FAILED
    assert calls == [workflow.workflow_id]


@pytest.mark.asyncio
async def test_cleanup_not_invoked_on_non_terminal_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "default_repo_url", "https://example.invalid/repo.git")

    def _fake_clone_repo(repo_url, run_id, ref=None):
        dest = tmp_path / run_id
        dest.mkdir(parents=True)
        return dest

    monkeypatch.setattr("app.orchestration.service.clone_repo", _fake_clone_repo)

    cleanup_calls = []
    monkeypatch.setattr("app.orchestration.service.cleanup_workspace", lambda workflow_id: cleanup_calls.append(workflow_id))

    import json

    from google.genai import types

    class _FakeEvent:
        def __init__(self, text):
            self.content = types.Content(role="model", parts=[types.Part(text=text)])

        def is_final_response(self):
            return True

    class _FakeSession:
        id = "s1"

    class _FakeSessionService:
        async def create_session(self, **kwargs):
            return _FakeSession()

    valid_plan = {
        "feature_summary": "x",
        "architecture_notes": "y",
        "affected_components": [{"name": "export", "reason": "add a format"}],
        "tasks": [{"id": "t1", "description": "d", "depends_on": []}],
        "dependencies": [],
        "acceptance_criteria": ["a"],
        "risks": [],
    }

    def make_fake_runner(text):
        async def _events(**kwargs):
            yield _FakeEvent(text)

        class _FakeRunner:
            def __init__(self, agent, app_name):
                self.session_service = _FakeSessionService()

            def run_async(self, **kwargs):
                return _events(**kwargs)

        return _FakeRunner

    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(valid_plan)))

    class _FakeJira:
        def create_story(self, summary, description):
            return {"key": "X-1", "url": "https://example.invalid/browse/X-1"}

    monkeypatch.setattr("app.agents.planning.JiraClient", _FakeJira)

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow()
    await workflow_repo.create(workflow)

    result = await service.execute_next_step(workflow.workflow_id)

    assert result.status == WorkflowStatus.PENDING
    assert result.current_stage == WorkflowStage.ARCHITECTURE
    assert cleanup_calls == []


@pytest.mark.asyncio
async def test_workspace_cleanup_can_be_disabled_for_debugging(monkeypatch):
    monkeypatch.setattr(get_settings(), "workspace_cleanup_enabled", False)
    calls = []
    monkeypatch.setattr("app.orchestration.service.cleanup_workspace", lambda workflow_id: calls.append(workflow_id))

    workflow_repo = InMemoryWorkflowRepository()
    service = make_service(workflow_repo)
    workflow = make_workflow()
    await workflow_repo.create(workflow)

    result = await service.execute_next_step(workflow.workflow_id)

    assert result.status == WorkflowStatus.FAILED
    assert calls == []  # cleanup skipped while disabled
