"""Real Firestore integration test — NOT part of the normal test run.

Skipped unless QUIPU_RUN_FIRESTORE_INTEGRATION_TESTS=true is set, and even
then requires real GCP credentials (Application Default Credentials) plus a
configured GCP_PROJECT_ID pointing at a project with Firestore enabled.
`pytest tests/` never triggers this file's network call. Creates and deletes
a throwaway workflow document; safe to run against a real project.
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QUIPU_RUN_FIRESTORE_INTEGRATION_TESTS") != "true",
    reason="set QUIPU_RUN_FIRESTORE_INTEGRATION_TESTS=true to run against a real Firestore project",
)


@pytest.mark.asyncio
async def test_real_firestore_workflow_round_trip():
    from app.domain import Ticket, WorkflowStage, WorkflowState
    from app.persistence.firestore import FirestoreWorkflowRepository, get_firestore_client

    client = get_firestore_client()
    repo = FirestoreWorkflowRepository(client)

    workflow = WorkflowState(
        workflow_id=f"quipu-integration-test-{uuid.uuid4()}",
        ticket=Ticket(title="integration test", description="throwaway"),
        current_stage=WorkflowStage.PLANNING,
    )
    try:
        await repo.create(workflow)
        fetched = await repo.get(workflow.workflow_id)
        assert fetched is not None
        assert fetched.workflow_id == workflow.workflow_id
    finally:
        await repo.delete(workflow.workflow_id)


@pytest.mark.asyncio
async def test_real_firestore_update_if_version_round_trip():
    """Exercises the actual transactional read-modify-write path
    (update_if_version -> _update_workflow_txn) against a real Firestore
    project — the exact path a mocked-transaction unit test cannot fully
    verify, since it depends on the installed google-cloud-firestore
    version's real AsyncTransaction/AsyncDocumentReference behavior (see
    _update_workflow_txn's docstring in app/persistence/firestore/
    repositories.py for the SDK-version-specific bug this guards
    against)."""
    from app.domain import Ticket, WorkflowStage, WorkflowState, WorkflowStatus
    from app.persistence.errors import VersionConflictError
    from app.persistence.firestore import FirestoreWorkflowRepository, get_firestore_client

    client = get_firestore_client()
    repo = FirestoreWorkflowRepository(client)

    workflow = WorkflowState(
        workflow_id=f"quipu-integration-test-{uuid.uuid4()}",
        ticket=Ticket(title="integration test", description="throwaway"),
        current_stage=WorkflowStage.PLANNING,
    )
    try:
        await repo.create(workflow)

        updated = workflow.model_copy(update={"status": WorkflowStatus.RUNNING})
        result = await repo.update_if_version(workflow.workflow_id, workflow.version, updated)
        assert result.version == workflow.version + 1
        assert result.status == WorkflowStatus.RUNNING

        fetched = await repo.get(workflow.workflow_id)
        assert fetched.status == WorkflowStatus.RUNNING
        assert fetched.version == workflow.version + 1

        # A stale expected_version must be rejected, not silently applied.
        with pytest.raises(VersionConflictError):
            await repo.update_if_version(workflow.workflow_id, workflow.version, updated)
    finally:
        await repo.delete(workflow.workflow_id)
