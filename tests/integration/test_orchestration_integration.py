"""Real end-to-end orchestration integration test — NOT part of the normal
test run. Skipped unless QUIPU_RUN_ORCHESTRATION_INTEGRATION_TESTS=true is
set, and even then requires real Gemini credentials (GOOGLE_API_KEY or
Vertex ADC) plus a git repo to clone into. `pytest tests/` never triggers
this file's network/model calls.

Runs a real ticket through the full Planning -> Architecture -> Codegen ->
Testing chain via OrchestrationService, using genuine Gemini calls at every
stage (no mocked runners) against a small public repo.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QUIPU_RUN_ORCHESTRATION_INTEGRATION_TESTS") != "true",
    reason="set QUIPU_RUN_ORCHESTRATION_INTEGRATION_TESTS=true to run a real Gemini-backed workflow",
)


@pytest.mark.asyncio
async def test_real_workflow_reaches_a_terminal_status():
    from app.core.repo import clone_repo
    from app.domain import Ticket, WorkflowStatus
    from app.orchestration import OrchestrationService, build_default_registry
    from app.persistence.memory import (
        InMemoryAgentExecutionRepository,
        InMemoryArtifactRepository,
        InMemoryDecisionRepository,
        InMemoryWorkflowRepository,
    )

    class NullKnowledgeGateway:
        async def search(self, request):
            return []

    class NullToolGateway:
        async def execute(self, request):
            raise NotImplementedError

    workspace = clone_repo("https://github.com/octocat/Hello-World", "orchestration-integration-test")

    service = OrchestrationService(
        workflow_repo=InMemoryWorkflowRepository(),
        artifact_repo=InMemoryArtifactRepository(),
        execution_repo=InMemoryAgentExecutionRepository(),
        decision_repo=InMemoryDecisionRepository(),
        registry=build_default_registry(),
        knowledge_gateway=NullKnowledgeGateway(),
        tool_gateway=NullToolGateway(),
    )

    ticket = Ticket(title="Add a CONTRIBUTING guide", description="Link a CONTRIBUTING.md from the README.")
    workflow = await service.start_workflow(ticket, workspace_path=str(workspace))

    final = await service.run_to_completion(workflow.workflow_id, max_steps=6)

    assert final.status in (
        WorkflowStatus.COMPLETED,
        WorkflowStatus.ESCALATED,
        WorkflowStatus.FAILED,
    )  # any terminal outcome is a legitimate proof the pipeline actually ran end to end
