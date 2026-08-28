"""Firestore repository tests — entirely against a fake in-memory client.
No network access, no real GCP project, no credentials required.
"""

import uuid

import pytest
from google.api_core import exceptions as google_exceptions

from app.domain import Artifact, ArtifactType, Decision, DecisionAction, DecisionSource, Ticket, WorkflowStage, WorkflowState
from app.persistence.errors import DuplicateEntityError, EntityNotFoundError, PersistenceError, VersionConflictError
from app.persistence.firestore.client import FirestoreConfigError, get_firestore_client
from app.persistence.firestore.repositories import (
    FirestoreArtifactRepository,
    FirestoreDecisionRepository,
    FirestoreWorkflowRepository,
    _update_workflow_txn,
)
from app.persistence.serialization import to_firestore_dict


# ---- fake Firestore client -------------------------------------------------


class _FakeSnapshot:
    def __init__(self, data, exists, doc_id):
        self._data = data
        self.exists = exists
        self.id = doc_id

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path
        self.id = path[-1]

    async def get(self):
        data = self._store.get(self._path)
        return _FakeSnapshot(dict(data) if data is not None else None, exists=data is not None, doc_id=self.id)

    async def set(self, data, merge=False):
        self._store[self._path] = dict(data)

    async def delete(self):
        self._store.pop(self._path, None)

    def collection(self, name):
        return _FakeCollectionRef(self._store, self._path + (name,))


class _FakeCollectionRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id=None):
        doc_id = doc_id or str(uuid.uuid4())
        return _FakeDocRef(self._store, self._path + (doc_id,))

    async def stream(self):
        prefix_len = len(self._path) + 1
        for path, data in list(self._store.items()):
            if len(path) == prefix_len and path[:-1] == self._path:
                yield _FakeSnapshot(dict(data), exists=True, doc_id=path[-1])


class _FakeTransaction:
    def __init__(self, store):
        self._store = store

    async def get(self, doc_ref):
        snapshot = await doc_ref.get()
        yield snapshot

    def set(self, doc_ref, data, merge=False):
        self._store[doc_ref._path] = dict(data)


class FakeFirestoreClient:
    def __init__(self):
        self.store: dict[tuple, dict] = {}

    def collection(self, name):
        return _FakeCollectionRef(self.store, (name,))

    def transaction(self):
        return _FakeTransaction(self.store)


class _RaisingDocRef:
    def __init__(self, exc: Exception):
        self._exc = exc
        self.id = "raising-doc"

    async def get(self):
        raise self._exc

    async def set(self, data, merge=False):
        raise self._exc


class _RaisingCollectionRef:
    def __init__(self, exc: Exception):
        self._exc = exc

    def document(self, doc_id=None):
        return _RaisingDocRef(self._exc)


class RaisingFirestoreClient:
    """A fake client whose every document operation raises the given error —
    for testing exception translation at the repository boundary."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def collection(self, name):
        return _RaisingCollectionRef(self._exc)


def make_workflow(**overrides) -> WorkflowState:
    defaults = dict(ticket=Ticket(title="Add dark mode", description="..."), current_stage=WorkflowStage.PLANNING)
    defaults.update(overrides)
    return WorkflowState(**defaults)


# 24. Firestore repository construction
def test_firestore_repository_construction():
    client = FakeFirestoreClient()
    repo = FirestoreWorkflowRepository(client)
    assert repo is not None


def test_get_firestore_client_requires_project_id(monkeypatch):
    from app import config as config_module
    from types import SimpleNamespace

    monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(gcp_project_id=None, firestore_database_id=None))
    with pytest.raises(FirestoreConfigError):
        get_firestore_client()


# 25. Firestore serialization (via a real save/get round trip against the fake)
@pytest.mark.asyncio
async def test_firestore_workflow_create_and_get_round_trip():
    client = FakeFirestoreClient()
    repo = FirestoreWorkflowRepository(client)
    workflow = make_workflow()
    await repo.create(workflow)
    fetched = await repo.get(workflow.workflow_id)
    assert fetched.workflow_id == workflow.workflow_id
    assert fetched.current_stage == WorkflowStage.PLANNING
    # confirm what's actually stored is Firestore-safe (enum -> str)
    stored = client.store[("workflows", workflow.workflow_id)]
    assert stored["current_stage"] == "planning"


@pytest.mark.asyncio
async def test_firestore_artifact_save_get_list():
    client = FakeFirestoreClient()
    repo = FirestoreArtifactRepository(client)
    artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent", payload={"tasks": []})
    await repo.save("wf-1", artifact)
    fetched = await repo.get("wf-1", artifact.artifact_id)
    assert fetched.artifact_id == artifact.artifact_id
    results = await repo.list_for_workflow("wf-1")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_firestore_decision_save_get_list():
    client = FakeFirestoreClient()
    repo = FirestoreDecisionRepository(client)
    decision = Decision(action=DecisionAction.CONTINUE, reason="ok", confidence=0.8, source=DecisionSource.ORCHESTRATOR)
    await repo.save("wf-1", decision)
    fetched = await repo.get("wf-1", decision.decision_id)
    assert fetched.action == DecisionAction.CONTINUE
    assert len(await repo.list_for_workflow("wf-1")) == 1


# 26. Firestore transaction update behavior using mocks/fakes
@pytest.mark.asyncio
async def test_update_workflow_txn_logic_directly():
    """Tests the transactional version-check-and-write logic directly against
    a fake transaction — the same logic FirestoreWorkflowRepository wraps
    with the real @firestore.async_transactional decorator in production."""
    client = FakeFirestoreClient()
    doc_ref = client.collection("workflows").document("wf-1")
    await doc_ref.set({"version": 1, "status": "pending"})

    transaction = client.transaction()
    new_version = await _update_workflow_txn(transaction, doc_ref, 1, {"status": "running"})
    assert new_version == 2
    stored = await doc_ref.get()
    assert stored.to_dict()["version"] == 2
    assert stored.to_dict()["status"] == "running"


@pytest.mark.asyncio
async def test_update_workflow_txn_version_conflict():
    client = FakeFirestoreClient()
    doc_ref = client.collection("workflows").document("wf-1")
    await doc_ref.set({"version": 3, "status": "pending"})

    transaction = client.transaction()
    with pytest.raises(VersionConflictError):
        await _update_workflow_txn(transaction, doc_ref, 1, {"status": "running"})


@pytest.mark.asyncio
async def test_update_workflow_txn_missing_workflow():
    client = FakeFirestoreClient()
    doc_ref = client.collection("workflows").document("does-not-exist")
    transaction = client.transaction()
    with pytest.raises(EntityNotFoundError):
        await _update_workflow_txn(transaction, doc_ref, 1, {"status": "running"})


# FirestoreWorkflowRepository.update_if_version() itself is NOT exercised
# end-to-end against the fake client: production wraps _update_workflow_txn
# with the real firestore.async_transactional decorator, whose __call__
# touches private AsyncTransaction internals (_read_only, _id, _begin, ...)
# that aren't part of the documented public API and shouldn't be mocked. The
# transactional logic itself — the part that actually matters — is fully
# covered by the three tests above, which call _update_workflow_txn directly.


# 27. Firestore exception translation
@pytest.mark.asyncio
async def test_firestore_permission_error_translated():
    client = RaisingFirestoreClient(google_exceptions.PermissionDenied("no access"))
    repo = FirestoreWorkflowRepository(client)
    with pytest.raises(PersistenceError):
        await repo.get("wf-1")


@pytest.mark.asyncio
async def test_firestore_unavailable_error_translated_on_create():
    client = RaisingFirestoreClient(google_exceptions.ServiceUnavailable("down"))
    repo = FirestoreWorkflowRepository(client)
    with pytest.raises(PersistenceError):
        await repo.create(make_workflow())


@pytest.mark.asyncio
async def test_firestore_duplicate_workflow_detected_without_raw_exception():
    client = FakeFirestoreClient()
    repo = FirestoreWorkflowRepository(client)
    workflow = make_workflow()
    await repo.create(workflow)
    with pytest.raises(DuplicateEntityError):
        await repo.create(workflow)
