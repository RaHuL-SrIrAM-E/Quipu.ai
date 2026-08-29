"""Dependency container — constructs the SAME repository/service objects
every other Quipu entrypoint (app.demo.harness.DemoHarness,
app.eventing.worker_main) already builds, reused unchanged here. This
module contains no business logic; it is wiring only, mirroring
DemoHarness's constructor exactly (Invariant 2/3: existing
services/repositories remain authoritative).

`build_memory_container()` — every repository in-memory, no credentials
required; this is what the test suite and local `uvicorn` runs without a
configured GCP project use. `build_firestore_container()` — the same
service objects, backed by the real Firestore repositories, used when
`Settings.gcp_project_id` is configured. Selecting between them is the
only decision this module makes.
"""

from dataclasses import dataclass

from app.agent_runtime.gateways.knowledge import KnowledgeServiceGateway
from app.config import get_settings
from app.domain import ToolExecution, ToolRequest
from app.feature_review import FeatureReviewService
from app.knowledge.backends.in_memory import InMemoryRetrievalBackend
from app.knowledge.service import LocalKnowledgeService
from app.orchestration.registry_setup import build_default_registry
from app.orchestration.service import OrchestrationService
from app.persistence.repositories.artifact import ArtifactRepository
from app.persistence.repositories.decision import DecisionRepository
from app.persistence.repositories.detection import DetectionRepository
from app.persistence.repositories.execution import AgentExecutionRepository
from app.persistence.repositories.feature_review import FeatureReviewRepository
from app.persistence.repositories.remediation_verification import RemediationVerificationRepository
from app.persistence.repositories.resolution import ResolutionRepository
from app.persistence.repositories.signal import SignalRepository
from app.persistence.repositories.workflow import WorkflowRepository


class _NoOpToolGateway:
    """OrchestrationService's AgentContext requires a ToolGateway (Level 1
    plumbing) — no production agent currently invokes context.tools.execute
    (every real tool call happens through an agent's own ADK
    before_tool_callback, not this gateway; see app.agent_runtime.gateways.
    tools), so a safe no-op default is correct here rather than
    constructing a real integration this API does not need."""

    async def execute(self, request: ToolRequest) -> ToolExecution:
        raise NotImplementedError("ToolGateway is not wired for API-driven workflow execution")


@dataclass
class ApiContainer:
    workflow_repo: WorkflowRepository
    artifact_repo: ArtifactRepository
    execution_repo: AgentExecutionRepository
    decision_repo: DecisionRepository
    signal_repo: SignalRepository
    detection_repo: DetectionRepository
    resolution_repo: ResolutionRepository
    verification_repo: RemediationVerificationRepository
    review_repo: FeatureReviewRepository
    orchestration: OrchestrationService
    review_service: FeatureReviewService


def _build_orchestration(
    *, workflow_repo, artifact_repo, execution_repo, decision_repo, review_repo, detection_repo, resolution_repo
) -> OrchestrationService:
    knowledge_gateway = KnowledgeServiceGateway(LocalKnowledgeService(InMemoryRetrievalBackend(documents=[], chunks=[])))
    return OrchestrationService(
        workflow_repo=workflow_repo,
        artifact_repo=artifact_repo,
        execution_repo=execution_repo,
        decision_repo=decision_repo,
        registry=build_default_registry(),
        knowledge_gateway=knowledge_gateway,
        tool_gateway=_NoOpToolGateway(),
        review_repo=review_repo,
        detection_repo=detection_repo,
        resolution_repo=resolution_repo,
    )


def build_memory_container(*, jira_client=None) -> ApiContainer:
    """`jira_client` is injectable so tests/local runs without Jira
    credentials can pass a fake satisfying `create_story(summary,
    description) -> dict` — the exact same seam
    FeatureReviewService/PlanningAgent already expose (jira_client=None
    means a real JiraClient is constructed lazily on first use, never at
    container-build time)."""
    from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
    from app.agent_runtime.gateways.signals import RepositorySignalGateway
    from app.persistence.memory.repositories import (
        InMemoryAgentExecutionRepository,
        InMemoryArtifactRepository,
        InMemoryDecisionRepository,
        InMemoryDetectionRepository,
        InMemoryFeatureReviewRepository,
        InMemoryRemediationVerificationRepository,
        InMemoryResolutionRepository,
        InMemorySignalRepository,
        InMemoryWorkflowRepository,
    )

    workflow_repo = InMemoryWorkflowRepository()
    artifact_repo = InMemoryArtifactRepository()
    execution_repo = InMemoryAgentExecutionRepository()
    decision_repo = InMemoryDecisionRepository()
    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    resolution_repo = InMemoryResolutionRepository()
    verification_repo = InMemoryRemediationVerificationRepository()
    review_repo = InMemoryFeatureReviewRepository()

    orchestration = _build_orchestration(
        workflow_repo=workflow_repo,
        artifact_repo=artifact_repo,
        execution_repo=execution_repo,
        decision_repo=decision_repo,
        review_repo=review_repo,
        detection_repo=detection_repo,
        resolution_repo=resolution_repo,
    )
    review_service = FeatureReviewService(
        review_repo, RepositoryDetectionGateway(detection_repo), RepositorySignalGateway(signal_repo), jira_client=jira_client
    )

    return ApiContainer(
        workflow_repo=workflow_repo,
        artifact_repo=artifact_repo,
        execution_repo=execution_repo,
        decision_repo=decision_repo,
        signal_repo=signal_repo,
        detection_repo=detection_repo,
        resolution_repo=resolution_repo,
        verification_repo=verification_repo,
        review_repo=review_repo,
        orchestration=orchestration,
        review_service=review_service,
    )


def build_firestore_container() -> ApiContainer:
    from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
    from app.agent_runtime.gateways.signals import RepositorySignalGateway
    from app.persistence.firestore.client import get_firestore_client
    from app.persistence.firestore.repositories import (
        FirestoreAgentExecutionRepository,
        FirestoreArtifactRepository,
        FirestoreDecisionRepository,
        FirestoreDetectionRepository,
        FirestoreFeatureReviewRepository,
        FirestoreRemediationVerificationRepository,
        FirestoreResolutionRepository,
        FirestoreSignalRepository,
        FirestoreWorkflowRepository,
    )

    client = get_firestore_client()
    workflow_repo = FirestoreWorkflowRepository(client)
    artifact_repo = FirestoreArtifactRepository(client)
    execution_repo = FirestoreAgentExecutionRepository(client)
    decision_repo = FirestoreDecisionRepository(client)
    signal_repo = FirestoreSignalRepository(client)
    detection_repo = FirestoreDetectionRepository(client)
    resolution_repo = FirestoreResolutionRepository(client)
    verification_repo = FirestoreRemediationVerificationRepository(client)
    review_repo = FirestoreFeatureReviewRepository(client)

    orchestration = _build_orchestration(
        workflow_repo=workflow_repo,
        artifact_repo=artifact_repo,
        execution_repo=execution_repo,
        decision_repo=decision_repo,
        review_repo=review_repo,
        detection_repo=detection_repo,
        resolution_repo=resolution_repo,
    )
    review_service = FeatureReviewService(review_repo, RepositoryDetectionGateway(detection_repo), RepositorySignalGateway(signal_repo))

    return ApiContainer(
        workflow_repo=workflow_repo,
        artifact_repo=artifact_repo,
        execution_repo=execution_repo,
        decision_repo=decision_repo,
        signal_repo=signal_repo,
        detection_repo=detection_repo,
        resolution_repo=resolution_repo,
        verification_repo=verification_repo,
        review_repo=review_repo,
        orchestration=orchestration,
        review_service=review_service,
    )


def build_default_container() -> ApiContainer:
    """Chooses Firestore when a GCP project is configured, in-memory
    otherwise — the same convention documented throughout app/config.py
    (every Google integration reuses gcp_project_id as its presence
    check)."""
    settings = get_settings()
    if settings.gcp_project_id:
        return build_firestore_container()
    return build_memory_container()
