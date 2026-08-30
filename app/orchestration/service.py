"""OrchestrationService — the framework-independent Quipu control plane.

Owns workflow progression, agent selection, artifact handoffs, decisions,
retry/replan routing, and durable workflow state. Agents own reasoning and
domain work; they never call each other — this is the only component that
routes work between them.

No google.adk import here — ADK-specific execution (the orchestration
decision agent) is called through app.orchestration.adk.propose_decision,
the one function that crosses that boundary. Firestore/in-memory persistence
comes in through the existing repository Protocols (app.persistence),
constructor-injected — this service never imports google.cloud.firestore
either.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.artifacts import RepositoryArtifactGateway
from app.agent_runtime.gateways.knowledge import KnowledgeGateway
from app.agent_runtime.gateways.tools import ToolGateway
from app.agent_runtime.registry import AgentNotFoundError, AgentRegistry
from app.agents.incident_resolution import STRATEGY_TARGET_AGENT
from app.config import get_settings
from app.core.observability import get_logger
from app.core.repo import RepoCloneError, cleanup_workspace, clone_repo
from app.domain import (
    AgentInput,
    ArtifactType,
    Decision,
    DecisionAction,
    DecisionSource,
    DetectionType,
    RemediationRisk,
    RemediationStrategy,
    ReviewStatus,
    Ticket,
    WorkflowStage,
    WorkflowState,
    WorkflowStatus,
)
from app.orchestration.adk import propose_decision
from app.orchestration.decisions import (
    ProposedDecision,
    WorkflowEvidence,
    build_decision,
    deployment_deterministic_action,
    deterministic_action,
)
from app.orchestration.errors import (
    InvalidTransitionError,
    OrchestrationError,
    RetryLimitExceededError,
    UnknownAgentError,
    WorkspaceProvisioningError,
)
from app.orchestration.transitions import (
    STAGE_INPUT_ARTIFACT_TYPE,
    STAGE_TO_AGENT_ID,
    STAGE_TO_ARTIFACT_TYPE,
    can_transition,
    next_stage,
)
from app.persistence.errors import VersionConflictError
from app.persistence.repositories.artifact import ArtifactRepository
from app.persistence.repositories.decision import DecisionRepository
from app.persistence.repositories.detection import DetectionRepository
from app.persistence.repositories.execution import AgentExecutionRepository
from app.persistence.repositories.feature_review import FeatureReviewRepository
from app.persistence.repositories.resolution import ResolutionRepository
from app.persistence.repositories.workflow import WorkflowRepository

logger = get_logger("quipu.orchestration.service")

_TERMINAL_STATUSES = {WorkflowStatus.COMPLETED, WorkflowStatus.ESCALATED, WorkflowStatus.CANCELLED, WorkflowStatus.FAILED}

# Only these stages actually read AgentInput.context["workspace_path"]
# (CodegenAgent/TestingAgent hard-require it; ArchitectureAgent uses it
# only if present; PlanningAgent's repo tools need it) — DeploymentAgent
# never references workspace_path at all (it only takes an image_tag), so
# provisioning a workspace for that stage would be pure waste and out of
# scope for this change (see the separate build/push/image_tag gap).
_STAGES_REQUIRING_WORKSPACE = {WorkflowStage.PLANNING, WorkflowStage.ARCHITECTURE, WorkflowStage.CODEGEN, WorkflowStage.TESTING}


class OrchestrationService:
    def __init__(
        self,
        *,
        workflow_repo: WorkflowRepository,
        artifact_repo: ArtifactRepository,
        execution_repo: AgentExecutionRepository,
        decision_repo: DecisionRepository,
        registry: AgentRegistry,
        knowledge_gateway: KnowledgeGateway,
        tool_gateway: ToolGateway,
        decision_runner_cls=None,
        review_repo: FeatureReviewRepository | None = None,
        detection_repo: DetectionRepository | None = None,
        resolution_repo: ResolutionRepository | None = None,
    ):
        self._workflow_repo = workflow_repo
        self._artifact_repo = artifact_repo
        self._execution_repo = execution_repo
        self._decision_repo = decision_repo
        self._registry = registry
        self._artifact_gateway = RepositoryArtifactGateway(artifact_repo)
        self._knowledge_gateway = knowledge_gateway
        self._tool_gateway = tool_gateway
        self._decision_runner_cls = decision_runner_cls  # injectable for tests; None -> propose_decision's own default
        # Optional (Level 3.5): only needed by start_workflow_from_review().
        # None is valid for every caller that doesn't use that entry point —
        # existing construction sites are unaffected.
        self._review_repo = review_repo
        # Optional (Level 3.6): only needed by start_remediation_from_resolution().
        # Same backward-compatible shape as review_repo above.
        self._detection_repo = detection_repo
        self._resolution_repo = resolution_repo

    # ---- public API -----------------------------------------------------

    async def start_workflow(
        self,
        ticket: Ticket,
        *,
        workspace_path: str | None = None,
        workflow_id: str | None = None,
        metadata: dict | None = None,
    ) -> WorkflowState:
        """The single workflow-creation entry point — start_workflow_from_review
        (Level 3.5) builds on this rather than duplicating it. `workflow_id`
        and `metadata` are additive, optional parameters: existing callers
        that only pass `ticket` (and maybe `workspace_path`) are unaffected.
        `workflow_id` lets a caller pre-claim an id (see
        start_workflow_from_review's idempotency design) instead of
        accepting WorkflowState's own default_factory uuid."""
        combined_metadata = dict(metadata or {})
        if workspace_path:
            combined_metadata["workspace_path"] = workspace_path
        kwargs: dict = dict(
            ticket=ticket, status=WorkflowStatus.PENDING, current_stage=WorkflowStage.PLANNING, metadata=combined_metadata
        )
        if workflow_id is not None:
            kwargs["workflow_id"] = workflow_id
        workflow = WorkflowState(**kwargs)
        await self._workflow_repo.create(workflow)
        logger.info("workflow %s started for ticket '%s'", workflow.workflow_id, ticket.title)
        return workflow

    async def start_workflow_from_review(self, review_id: str) -> WorkflowState:
        """Level 3.5 — the Feature Review -> SDLC entry point (§2-4 of the
        task). Deliberately lives here, not in FeatureReviewService:
        FeatureReviewService owns the review/ticket decision, this service
        owns workflow execution — see docs/architecture/feature_to_sdlc.md
        "Responsibility boundary". Requires only a FeatureReviewRepository
        (constructor-injected); never invokes PlanningAgent directly — it
        creates a WorkflowState at WorkflowStage.PLANNING exactly like
        start_workflow() always has, and execute_next_step() (unchanged)
        is what actually runs Planning, through the same AgentRegistry/
        AgentInput/AgentContext/capability path every other workflow uses.

        Validates (§22 adversarial A/B): the review must exist, be
        APPROVED, and have an associated ticket — a PENDING or REJECTED
        review, or one somehow missing its ticket, raises OrchestrationError
        rather than starting anything. Idempotent (§10/§11): a review that
        already has a workflow_id returns that existing WorkflowState (or,
        if the claim succeeded but the workflow was never actually created
        — e.g. a crash in between — creates it now using the already-claimed
        id, rather than either erroring forever or claiming a second id).
        Concurrency-safe via FeatureReviewRepository.update_if_version: two
        simultaneous callers can both read workflow_id=None, but only one
        can win the version-checked claim write; the loser re-reads and
        returns the winner's workflow. See docs/architecture/feature_to_sdlc.md
        "Idempotency and concurrency" for the documented limitation this
        does NOT claim to solve (a crash after the claim write but before
        WorkflowRepository.create ever runs is recovered by the next call,
        not by this one)."""
        if self._review_repo is None:
            raise OrchestrationError("FeatureReviewRepository is not configured — cannot start a workflow from a review")

        review = await self._review_repo.get(review_id)
        if review is None:
            raise OrchestrationError(f"FeatureReview '{review_id}' not found")
        if review.status != ReviewStatus.APPROVED:
            raise OrchestrationError(f"FeatureReview '{review_id}' is not approved (status='{review.status.value}')")
        if review.ticket is None:
            raise OrchestrationError(f"FeatureReview '{review_id}' has no associated ticket")

        review_metadata = {"source": "feature_review", "review_id": review.review_id, "source_detection_id": review.detection_id}

        if review.workflow_id is not None:
            existing = await self._workflow_repo.get(review.workflow_id)
            if existing is not None:
                return existing
            logger.info("workflow %s was claimed by FeatureReview %s but never created — creating it now", review.workflow_id, review_id)
            return await self.start_workflow(review.ticket, workflow_id=review.workflow_id, metadata=review_metadata)

        candidate_workflow_id = str(uuid.uuid4())
        claim = review.model_copy(update={"workflow_id": candidate_workflow_id})
        try:
            await self._review_repo.update_if_version(review_id, review.version, claim)
        except VersionConflictError:
            current = await self._review_repo.get(review_id)
            if current is not None and current.workflow_id is not None:
                existing = await self._workflow_repo.get(current.workflow_id)
                if existing is not None:
                    return existing
            raise

        workflow = await self.start_workflow(review.ticket, workflow_id=candidate_workflow_id, metadata=review_metadata)
        logger.info(
            "workflow %s started from approved FeatureReview %s (detection %s)", workflow.workflow_id, review_id, review.detection_id
        )
        return workflow

    async def start_remediation_from_resolution(self, resolution_id: str) -> WorkflowState:
        """Level 3.6 — the Incident Resolution -> authorized remediation
        entry point. "Gemini recommends, application policy authorizes,
        the orchestrator executes, agents perform the work" — see
        docs/architecture/incident_remediation.md.

        Deliberately does NOT create a new WorkflowState. CodegenAgent and
        ArchitectureAgent both hard-require their upstream artifact
        (ArchitectureArtifact / PlanArtifact respectively — see
        app.agents.codegen/app.agents.architecture), and Artifact storage
        is workflow-scoped (workflows/{workflow_id}/artifacts/{id}). The
        only place those artifacts already exist is the ORIGINAL workflow
        that deployed the code now causing the incident — identified by
        ResolutionResult.workflow_id (set by IncidentResolutionAgent from
        its own AgentInput.workflow_id). So remediation *reopens* that
        workflow (COMPLETED -> PENDING, current_stage jumped to the entry
        stage for the authorized strategy) rather than inventing a new
        WorkflowState/IncidentWorkflow concept — every existing mechanism
        (execute_next_step, _reconcile_stage crash recovery, retry
        budgets, transition policy, the Testing/Deployment evidence gates)
        then applies completely unchanged, because it's the same
        WorkflowState machinery every other workflow already uses.

        Authorization (§13 of the task) is re-derived deterministically
        here, never trusted from the persisted ResolutionResult alone —
        this is a defense-in-depth backstop re-checking the exact
        invariants IncidentResolutionAgent's own _apply_safety_policy
        already enforces before ever persisting a non-escalation
        ResolutionResult (Level 3.3), not a second, competing risk policy.
        `resolution.target_agent` is never read at all — the entry stage
        is derived purely from `remediation_strategy` via the SAME
        STRATEGY_TARGET_AGENT map IncidentResolutionAgent itself uses,
        reusing `_stage_for_agent()` already defined below.

        Idempotent per resolution_id (§23) and concurrency-safe via
        WorkflowRepository.update_if_version (§25) — see the module-level
        tests in tests/test_incident_remediation.py for the full matrix.
        """
        if self._resolution_repo is None:
            raise OrchestrationError("ResolutionRepository is not configured — cannot start remediation")
        if self._detection_repo is None:
            raise OrchestrationError("DetectionRepository is not configured — cannot start remediation")

        resolution = await self._resolution_repo.get(resolution_id)
        if resolution is None:
            raise OrchestrationError(f"ResolutionResult '{resolution_id}' not found")

        detection = await self._detection_repo.get(resolution.detection_id)
        if detection is None or detection.detection_type != DetectionType.INCIDENT:
            raise OrchestrationError(
                f"ResolutionResult '{resolution_id}' is not associated with a valid INCIDENT DetectionResult"
            )

        if resolution.workflow_id is None:
            raise OrchestrationError(
                f"ResolutionResult '{resolution_id}' has no associated workflow_id — cannot locate the artifacts to remediate"
            )
        workflow = await self._workflow_repo.get(resolution.workflow_id)
        if workflow is None:
            raise OrchestrationError(f"workflow '{resolution.workflow_id}' referenced by ResolutionResult '{resolution_id}' not found")

        actioned = workflow.metadata.get("remediation_resolution_ids", [])
        if resolution_id in actioned:
            return workflow  # idempotent — already actioned on this workflow, no duplicate execution

        if workflow.status != WorkflowStatus.COMPLETED:
            raise OrchestrationError(
                f"workflow '{workflow.workflow_id}' is not COMPLETED (status='{workflow.status.value}') — cannot start remediation"
            )

        strategy = self._authorize_remediation_strategy(resolution)

        new_metadata = {
            **workflow.metadata,
            "remediation_resolution_ids": [*actioned, resolution_id],
            "remediation_detection_id": resolution.detection_id,
            "remediation_strategy": strategy.value,
        }

        if strategy == RemediationStrategy.ESCALATE:
            updated = workflow.model_copy(update={"status": WorkflowStatus.ESCALATED, "metadata": new_metadata})
            result = await self._commit_remediation_update(workflow, updated, resolution_id)
            logger.info("workflow %s escalated for remediation of resolution %s", workflow.workflow_id, resolution_id)
            return result

        if strategy == RemediationStrategy.NO_ACTION:
            updated = workflow.model_copy(update={"metadata": new_metadata})
            result = await self._commit_remediation_update(workflow, updated, resolution_id)
            logger.info("workflow %s: no remediation action authorized for resolution %s", workflow.workflow_id, resolution_id)
            return result

        # CODE_FIX / ARCHITECTURE_REVIEW — reopen the workflow at the
        # deterministically-derived entry stage. target_agent is derived
        # from `strategy`, never read from `resolution.target_agent`.
        target_agent = STRATEGY_TARGET_AGENT[strategy]
        entry_stage = self._stage_for_agent(target_agent)
        updated = workflow.model_copy(update={"status": WorkflowStatus.PENDING, "current_stage": entry_stage, "metadata": new_metadata})
        result = await self._commit_remediation_update(workflow, updated, resolution_id)
        logger.info(
            "workflow %s reopened at stage %s for %s remediation (resolution %s)",
            workflow.workflow_id,
            entry_stage.value,
            strategy.value,
            resolution_id,
        )
        return result

    def _authorize_remediation_strategy(self, resolution) -> RemediationStrategy:
        """Deterministic re-authorization backstop (§13 A-E). Can only ever
        downgrade toward ESCALATE — never upgrade or invent a more
        permissive outcome than what was already persisted. ROLLBACK is
        deliberately never auto-executed regardless of how it validates —
        see docs/architecture/incident_remediation.md 'Rollback behavior'
        for why no safe automated Cloud Run rollback exists yet."""
        strategy = resolution.remediation_strategy

        if strategy == RemediationStrategy.ROLLBACK:
            return RemediationStrategy.ESCALATE

        if strategy in (RemediationStrategy.ESCALATE, RemediationStrategy.NO_ACTION):
            return strategy

        settings = get_settings()
        if resolution.risk == RemediationRisk.HIGH:
            return RemediationStrategy.ESCALATE
        if resolution.root_cause_confidence < settings.incident_resolution_min_confidence_for_auto_remediation:
            return RemediationStrategy.ESCALATE
        if not resolution.supporting_signal_ids:
            return RemediationStrategy.ESCALATE

        return strategy

    async def _commit_remediation_update(self, workflow: WorkflowState, updated: WorkflowState, resolution_id: str) -> WorkflowState:
        """The atomic claim step (§25): only one of two concurrent
        start_remediation_from_resolution calls for the same resolution_id
        can win this write. The loser re-reads and returns the winner's
        state if it already reflects this resolution_id having been
        actioned — never silently duplicating remediation."""
        try:
            return await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, updated)
        except VersionConflictError:
            current = await self._workflow_repo.get(workflow.workflow_id)
            if current is not None and resolution_id in current.metadata.get("remediation_resolution_ids", []):
                return current
            raise

    async def retry_failed_workflow(self, workflow_id: str) -> WorkflowState:
        """Reopens a FAILED WorkflowState in place so it can be re-executed
        from exactly the stage it failed at — the same workflow_id, same
        artifact_ids/execution_ids, same FeatureReview.workflow_id pointer.
        Never creates a second WorkflowState (FeatureReview.workflow_id is
        a single idempotency pointer, untouched here — see
        app/domain/feature_review.py).

        Structurally the same "reopen, don't recreate" mechanism
        start_remediation_from_resolution already uses for a COMPLETED
        workflow, just simpler: there is no strategy/target-agent to
        derive here — the resume stage is exactly workflow.current_stage,
        already correctly recorded by _fail_workflow, so current_stage is
        never modified. artifact_ids/execution_ids/active_decision_id/
        active_incident_ids are all left exactly as they are — Planning
        and Architecture's existing artifacts are already there, and
        execute_next_step()'s existing _resolve_input_artifact_id/
        _reconcile_stage machinery (unchanged) picks them up automatically
        the next time it runs. If the workspace this workflow was using
        was already reclaimed (WorkspaceProvisioningError/cleanup on
        FAILED), _ensure_workspace() — also unchanged — transparently
        re-provisions a fresh one on the next execute_next_step() call;
        this method does not touch the workspace at all.

        Concurrency-safe via WorkflowRepository.update_if_version: of two
        simultaneous retries, only one wins the version-checked write; the
        loser re-reads and returns the winner's now-PENDING state instead
        of erroring or retrying itself — exactly one WorkflowState/
        workflow_id ever exists, and retrying never creates a duplicate
        artifact or execution on its own (only running the resumed stage
        does that, same as any other execute_next_step() call)."""
        workflow = await self._get_workflow_or_raise(workflow_id)

        if workflow.status != WorkflowStatus.FAILED:
            raise OrchestrationError(f"workflow '{workflow_id}' is not FAILED (status='{workflow.status.value}') — cannot retry")

        retry_count = workflow.metadata.get("retry_count", 0) + 1
        updated = workflow.model_copy(
            update={
                "status": WorkflowStatus.PENDING,
                "metadata": {
                    **workflow.metadata,
                    "retry_count": retry_count,
                    "last_retried_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        )
        try:
            result = await self._workflow_repo.update_if_version(workflow_id, workflow.version, updated)
            logger.info("workflow %s retried at stage %s (retry_count=%d)", workflow_id, workflow.current_stage.value, retry_count)
            return result
        except VersionConflictError:
            current = await self._get_workflow_or_raise(workflow_id)
            if current.status == WorkflowStatus.PENDING:
                return current  # someone else's concurrent retry already won — idempotent re-entry
            raise

    async def execute_next_step(self, workflow_id: str) -> WorkflowState:
        """Runs exactly one stage forward (delegating to _run_next_step),
        then reclaims this workflow's local workspace the moment it reaches
        a terminal status — a single choke point that covers every exit
        path _run_next_step has (fail, complete, escalate), rather than
        duplicating a cleanup call at each one."""
        result = await self._run_next_step(workflow_id)
        if result.status in _TERMINAL_STATUSES:
            await self._maybe_cleanup_workspace(result.workflow_id)
        return result

    async def _run_next_step(self, workflow_id: str) -> WorkflowState:
        """Runs exactly one stage forward, or reconciles already-completed
        durable evidence instead of re-running it. Safe to call repeatedly —
        this IS the resume/recovery mechanism (see resume_workflow)."""
        workflow = await self._get_workflow_or_raise(workflow_id)

        if workflow.status in _TERMINAL_STATUSES:
            return workflow

        stage = workflow.current_stage
        if stage not in STAGE_TO_AGENT_ID:
            raise InvalidTransitionError(f"stage '{stage}' has no registered agent (Deployment/Monitoring/... not implemented yet)")

        reconciled = await self._reconcile_stage(workflow, stage)
        if reconciled is not None:
            return reconciled

        agent_id = STAGE_TO_AGENT_ID[stage]
        try:
            quipu_agent = self._registry.get(agent_id)
        except AgentNotFoundError as exc:
            raise UnknownAgentError(agent_id) from exc

        if stage in _STAGES_REQUIRING_WORKSPACE:
            try:
                await self._ensure_workspace(workflow)
            except WorkspaceProvisioningError as exc:
                return await self._fail_workflow(workflow, str(exc))
            # _ensure_workspace may have persisted a new workspace_path
            # (version bump) — re-read so _build_agent_input sees it and
            # the RUNNING transition below uses the current version.
            workflow = await self._get_workflow_or_raise(workflow_id)

        input_artifact_id = await self._resolve_input_artifact_id(workflow, stage)
        execution_id = str(uuid.uuid4())
        agent_input = self._build_agent_input(workflow, agent_id, input_artifact_id, execution_id)
        context = self._agent_context(workflow, execution_id)

        workflow = await self._workflow_repo.update_if_version(
            workflow_id, workflow.version, workflow.model_copy(update={"status": WorkflowStatus.RUNNING})
        )

        logger.info("workflow %s: invoking %s (execution %s)", workflow_id, agent_id, execution_id)
        output = await quipu_agent.execute(agent_input, context)

        if output.status != WorkflowStatus.COMPLETED:
            reason = output.errors[0].message if output.errors else "agent execution failed"
            return await self._fail_workflow(workflow, reason)

        if not output.artifacts:
            return await self._fail_workflow(workflow, f"{agent_id} completed without producing an artifact")

        artifact = output.artifacts[0]
        expected_type = STAGE_TO_ARTIFACT_TYPE[stage]
        if artifact.artifact_type != expected_type:
            return await self._fail_workflow(
                workflow, f"expected artifact type '{expected_type}' from {agent_id}, got '{artifact.artifact_type}'"
            )

        if stage == WorkflowStage.TESTING:
            return await self._handle_testing_result(workflow, artifact, execution_id)
        if stage == WorkflowStage.DEPLOYMENT:
            return await self._handle_deployment_result(workflow, artifact, execution_id)

        return await self._advance_to_next_stage(workflow, artifact, execution_id)

    async def handle_decision(
        self, workflow_id: str, proposed: ProposedDecision, *, source: DecisionSource = DecisionSource.AGENT
    ) -> WorkflowState:
        """Validates a proposed decision against the transition policy and
        retry budget, persists the authoritative Decision, and executes the
        resulting action. An invalid proposal is never silently followed —
        it's downgraded to ESCALATE with the orchestrator as the source."""
        workflow = await self._get_workflow_or_raise(workflow_id)

        try:
            can_transition(workflow.current_stage, proposed.action, proposed.target_agent)
            if proposed.action in (DecisionAction.RETRY, DecisionAction.REPLAN):
                self._check_retry_budget(workflow, proposed.target_agent)
        except InvalidTransitionError as exc:
            logger.warning("workflow %s: rejecting proposed decision (%s) — escalating instead", workflow_id, exc)
            proposed = ProposedDecision(action=DecisionAction.ESCALATE, reason=f"rejected: {exc}", confidence=1.0)
            source = DecisionSource.ORCHESTRATOR

        decision = build_decision(proposed, source=source)
        await self._decision_repo.save(workflow_id, decision)
        workflow = await self._workflow_repo.update_if_version(
            workflow_id, workflow.version, workflow.model_copy(update={"active_decision_id": decision.decision_id})
        )

        return await self._execute_decision(workflow, decision)

    async def resume_workflow(self, workflow_id: str) -> WorkflowState:
        """Reconciles durable evidence before doing anything else — see
        execute_next_step's call to _reconcile_stage. Never blindly re-runs
        a stage whose AgentExecution already completed."""
        return await self.execute_next_step(workflow_id)

    async def recover_workflow(self, workflow_id: str) -> WorkflowState:
        return await self.resume_workflow(workflow_id)

    async def run_to_completion(self, workflow_id: str, *, max_steps: int = 20) -> WorkflowState:
        """Convenience: repeatedly calls execute_next_step until the workflow
        reaches a terminal status or max_steps is hit (never unbounded)."""
        workflow = await self._get_workflow_or_raise(workflow_id)
        for _ in range(max_steps):
            if workflow.status in _TERMINAL_STATUSES:
                return workflow
            workflow = await self.execute_next_step(workflow_id)
        return workflow

    # ---- internals --------------------------------------------------------

    async def _resolve_input_artifact_id(self, workflow: WorkflowState, stage: WorkflowStage) -> str | None:
        """Which artifact this stage actually consumes — NOT necessarily the
        most recently produced one (Deployment needs the CodeArtifact, not
        the TestArtifact Testing just produced). Scans workflow.artifact_ids
        newest-first for the first one matching the stage's declared input
        type (see STAGE_INPUT_ARTIFACT_TYPE)."""
        expected_type = STAGE_INPUT_ARTIFACT_TYPE.get(stage)
        if expected_type is None:
            return None
        for artifact_id in reversed(workflow.artifact_ids):
            artifact = await self._artifact_repo.get(workflow.workflow_id, artifact_id)
            if artifact is not None and artifact.artifact_type == expected_type:
                return artifact_id
        return None

    async def _ensure_workspace(self, workflow: WorkflowState) -> str:
        """Resolves a repo_url/ref for `workflow`, ensures a real
        checked-out workspace exists for it on THIS instance's local disk,
        and persists the result into workflow.metadata["workspace_path"]
        via the existing optimistic-concurrency update path — the same
        mechanism every other WorkflowState mutation in this service uses,
        not a new persistence path.

        Resolution order (never inferred from Jira/DetectionResult/Signal —
        none of those carry repository identity): a per-Ticket override
        (workflow.ticket.metadata["repo_url"]/["repo_ref"]), else
        Settings.default_repo_url/default_repo_ref. Raises
        WorkspaceProvisioningError — never lets a workspace-requiring stage
        start silently without one — when no repo_url resolves at all, or
        the actual `git clone` (app.core.repo.clone_repo) fails.

        Isolated per workflow_id: clone_repo(repo_url, workflow.workflow_id,
        ...) namespaces into <workspace_root>/<workflow_id>, never a shared
        directory, so concurrent workflows on the same instance can never
        collide. Reuses an existing workspace_path as long as it still
        exists as a real directory on THIS instance's disk — checked
        FIRST, before any repo_url resolution, so a workflow that already
        has a valid workspace (e.g. one supplied directly to
        start_workflow(), the way tests/demo scenarios already do) never
        needs repository configuration at all. Cloud Run's filesystem is
        ephemeral, so a workflow resumed on a different instance (e.g. a
        later /step call) transparently re-clones rather than trusting a
        stale path, at the cost of losing any uncommitted in-workspace
        changes from the prior instance. /run drives an entire workflow to
        completion within one synchronous request on one instance, so this
        only matters for /step-by-/step callers — a known, accepted
        limitation for this hackathon's scope, not something this change
        claims to solve."""
        existing_path = workflow.metadata.get("workspace_path")
        if existing_path and Path(existing_path).is_dir():
            return existing_path

        settings = get_settings()
        repo_url = workflow.ticket.metadata.get("repo_url") or settings.default_repo_url
        if not repo_url:
            raise WorkspaceProvisioningError(
                f"no repository configured for workflow '{workflow.workflow_id}' — set Ticket.metadata['repo_url'] "
                "or Settings.default_repo_url before Planning/Architecture/Codegen/Testing can run"
            )
        ref = workflow.ticket.metadata.get("repo_ref") or settings.default_repo_ref

        try:
            workspace_path = str(clone_repo(repo_url, workflow.workflow_id, ref=ref))
        except RepoCloneError as exc:
            raise WorkspaceProvisioningError(
                f"failed to provision a workspace for workflow '{workflow.workflow_id}': {exc}"
            ) from exc

        updated = workflow.model_copy(update={"metadata": {**workflow.metadata, "workspace_path": workspace_path}})
        await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, updated)
        logger.info("workflow %s: workspace provisioned at %s", workflow.workflow_id, workspace_path)
        return workspace_path

    async def _maybe_cleanup_workspace(self, workflow_id: str) -> None:
        """Reclaims a terminal workflow's local workspace — never its
        durable Firestore artifacts, which live entirely independently in
        ArtifactRepository. Gated by Settings.workspace_cleanup_enabled so
        a debugging session can temporarily disable reclamation to inspect
        a failed run's checked-out workspace; leave enabled otherwise, since
        ephemeral Cloud Run disk is not a place to accumulate workspaces."""
        if not get_settings().workspace_cleanup_enabled:
            return
        cleanup_workspace(workflow_id)

    def _build_agent_input(self, workflow: WorkflowState, agent_id: str, input_artifact_id: str | None, execution_id: str) -> AgentInput:
        context: dict = {}
        workspace_path = workflow.metadata.get("workspace_path")
        if workspace_path:
            context["workspace_path"] = workspace_path
        return AgentInput(
            execution_id=execution_id,
            workflow_id=workflow.workflow_id,
            agent_name=agent_id,
            ticket=workflow.ticket,
            artifact_ids=[input_artifact_id] if input_artifact_id else [],
            context=context,
        )

    def _agent_context(self, workflow: WorkflowState, execution_id: str) -> AgentContext:
        return AgentContext(
            workflow_id=workflow.workflow_id,
            execution_id=execution_id,
            knowledge=self._knowledge_gateway,
            tools=self._tool_gateway,
            artifacts=self._artifact_gateway,
            executions=self._execution_repo,
        )

    async def _reconcile_stage(self, workflow: WorkflowState, stage: WorkflowStage) -> WorkflowState | None:
        """Idempotency/crash-recovery: if this stage already has a COMPLETED
        AgentExecution whose output artifact isn't yet reflected in workflow
        state, advance from that evidence instead of re-invoking the agent."""
        agent_id = STAGE_TO_AGENT_ID[stage]
        executions = await self._execution_repo.list_for_workflow(workflow.workflow_id)
        completed = [e for e in executions if e.agent_name == agent_id and e.status == WorkflowStatus.COMPLETED and e.output_artifact_ids]
        if not completed:
            return None

        latest = max(completed, key=lambda e: e.started_at)
        artifact_id = latest.output_artifact_ids[0]
        if artifact_id in workflow.artifact_ids:
            return None  # already advanced past this — nothing to reconcile

        artifact = await self._artifact_repo.get(workflow.workflow_id, artifact_id)
        if artifact is None:
            return None  # execution says it produced this artifact, but it's gone — fall through to a fresh run

        logger.info(
            "workflow %s: reconciling completed %s execution (artifact %s) not yet reflected in workflow state",
            workflow.workflow_id,
            agent_id,
            artifact_id,
        )
        if stage == WorkflowStage.TESTING:
            return await self._handle_testing_result(workflow, artifact, latest.execution_id)
        if stage == WorkflowStage.DEPLOYMENT:
            return await self._handle_deployment_result(workflow, artifact, latest.execution_id)
        return await self._advance_to_next_stage(workflow, artifact, latest.execution_id)

    async def _advance_to_next_stage(self, workflow: WorkflowState, artifact, execution_id: str) -> WorkflowState:
        nxt = next_stage(workflow.current_stage)
        updated = workflow.model_copy(
            update={
                "status": WorkflowStatus.PENDING if nxt else WorkflowStatus.COMPLETED,
                "current_stage": nxt or WorkflowStage.COMPLETED,
                "artifact_ids": [*workflow.artifact_ids, artifact.artifact_id],
                "execution_ids": [*workflow.execution_ids, execution_id],
            }
        )
        return await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, updated)

    async def _fail_workflow(self, workflow: WorkflowState, reason: str) -> WorkflowState:
        logger.warning("workflow %s failed at stage %s: %s", workflow.workflow_id, workflow.current_stage, reason)
        updated = workflow.model_copy(
            update={"status": WorkflowStatus.FAILED, "metadata": {**workflow.metadata, "failure_reason": reason}}
        )
        return await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, updated)

    async def _handle_testing_result(self, workflow: WorkflowState, test_artifact, execution_id: str) -> WorkflowState:
        # Durable evidence first, verdict second — the artifact/execution
        # reference is recorded regardless of what the decision engine does next.
        workflow = workflow.model_copy(
            update={
                "artifact_ids": [*workflow.artifact_ids, test_artifact.artifact_id],
                "execution_ids": [*workflow.execution_ids, execution_id],
            }
        )
        workflow = await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, workflow)

        overall_status = test_artifact.payload.get("overall_status")
        if overall_status == "passed":
            proposed = ProposedDecision(action=DecisionAction.CONTINUE, reason="all executed tests passed", confidence=1.0)
            return await self.handle_decision(workflow.workflow_id, proposed, source=DecisionSource.ORCHESTRATOR)

        classifications = [f.get("classification") for f in test_artifact.payload.get("failures", [])]
        deterministic = deterministic_action(classifications)
        if deterministic is not None:
            action, target = deterministic
            proposed = ProposedDecision(
                action=action, target_agent=target, reason=f"deterministic routing for classification(s) {classifications}", confidence=0.9
            )
            return await self.handle_decision(workflow.workflow_id, proposed, source=DecisionSource.ORCHESTRATOR)

        evidence = WorkflowEvidence(
            workflow_id=workflow.workflow_id,
            current_stage=workflow.current_stage,
            test_status=overall_status,
            failure_classifications=classifications,
            retry_count=self._retry_count(workflow, "testing_agent"),
            max_retries=self._max_retries_for(WorkflowStage.TESTING),
            summary=test_artifact.payload.get("summary", ""),
        )
        kwargs = {"runner_cls": self._decision_runner_cls} if self._decision_runner_cls is not None else {}
        proposed = await propose_decision(evidence, **kwargs)
        return await self.handle_decision(workflow.workflow_id, proposed, source=DecisionSource.AGENT)

    async def _handle_deployment_result(self, workflow: WorkflowState, deployment_artifact, execution_id: str) -> WorkflowState:
        # Durable evidence first, verdict second — same pattern as Testing.
        workflow = workflow.model_copy(
            update={
                "artifact_ids": [*workflow.artifact_ids, deployment_artifact.artifact_id],
                "execution_ids": [*workflow.execution_ids, execution_id],
            }
        )
        workflow = await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, workflow)

        status = deployment_artifact.payload.get("status")
        if status == "succeeded":
            proposed = ProposedDecision(action=DecisionAction.CONTINUE, reason="deployment succeeded", confidence=1.0)
            return await self.handle_decision(workflow.workflow_id, proposed, source=DecisionSource.ORCHESTRATOR)

        # Deployment failures are never ambiguous the way a mixed Testing
        # failure set can be — always deterministic, never routed to the
        # orchestration LlmAgent. See app.orchestration.decisions.
        classification = deployment_artifact.payload.get("failure_classification")
        action, target = deployment_deterministic_action(classification)
        proposed = ProposedDecision(
            action=action, target_agent=target, reason=f"deterministic routing for deployment classification '{classification}'", confidence=0.9
        )
        return await self.handle_decision(workflow.workflow_id, proposed, source=DecisionSource.ORCHESTRATOR)

    async def _execute_decision(self, workflow: WorkflowState, decision: Decision) -> WorkflowState:
        if decision.action == DecisionAction.CONTINUE:
            nxt = next_stage(workflow.current_stage)
            if nxt:
                update = {"status": WorkflowStatus.PENDING, "current_stage": nxt}
            else:
                update = {"status": WorkflowStatus.COMPLETED, "current_stage": WorkflowStage.COMPLETED}
                if workflow.metadata.get("remediation_resolution_ids"):
                    # Level 3.6 (§20/§21): deployment succeeding is NOT the
                    # same fact as "the incident is resolved" — that
                    # requires Monitoring/Detecting to actually observe
                    # post-deployment signals, which is explicitly out of
                    # scope for this synchronous completion step (§22: no
                    # recursive Detecting->Resolution loop here). Recorded
                    # via existing metadata, no new WorkflowStatus value.
                    update["metadata"] = {**workflow.metadata, "remediation_outcome": "deployed_pending_verification"}
            return await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, workflow.model_copy(update=update))

        if decision.action == DecisionAction.COMPLETE:
            update = {"status": WorkflowStatus.COMPLETED, "current_stage": WorkflowStage.COMPLETED}
            return await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, workflow.model_copy(update=update))

        if decision.action in (DecisionAction.RETRY, DecisionAction.REPLAN):
            target_stage = self._stage_for_agent(decision.target_agent)
            retry_key = f"retry_count:{decision.target_agent}"
            metadata = {**workflow.metadata, retry_key: workflow.metadata.get(retry_key, 0) + 1}
            update = {"status": WorkflowStatus.PENDING, "current_stage": target_stage, "metadata": metadata}
            return await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, workflow.model_copy(update=update))

        if decision.action == DecisionAction.ESCALATE:
            update = {"status": WorkflowStatus.ESCALATED}
            return await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, workflow.model_copy(update=update))

        if decision.action == DecisionAction.FAIL:
            update = {"status": WorkflowStatus.FAILED}
            return await self._workflow_repo.update_if_version(workflow.workflow_id, workflow.version, workflow.model_copy(update=update))

        raise InvalidTransitionError(f"unhandled action '{decision.action}'")

    def _stage_for_agent(self, agent_id: str | None) -> WorkflowStage:
        for stage, aid in STAGE_TO_AGENT_ID.items():
            if aid == agent_id:
                return stage
        raise InvalidTransitionError(f"no stage maps to agent '{agent_id}'")

    def _retry_count(self, workflow: WorkflowState, agent_id: str) -> int:
        return workflow.metadata.get(f"retry_count:{agent_id}", 0)

    def _max_retries_for(self, stage: WorkflowStage) -> int:
        from app.config import get_settings

        settings = get_settings()
        return {
            WorkflowStage.CODEGEN: settings.max_codegen_retries,
            WorkflowStage.TESTING: settings.max_test_retries,
            WorkflowStage.ARCHITECTURE: settings.max_architecture_replans,
            WorkflowStage.DEPLOYMENT: settings.max_deployment_retries,
        }.get(stage, 0)

    def _check_retry_budget(self, workflow: WorkflowState, target_agent: str | None) -> None:
        if target_agent is None:
            return
        target_stage = self._stage_for_agent(target_agent)
        limit = self._max_retries_for(target_stage)
        current = self._retry_count(workflow, target_agent)
        if current >= limit:
            raise RetryLimitExceededError(target_stage.value, limit)

    async def _get_workflow_or_raise(self, workflow_id: str) -> WorkflowState:
        workflow = await self._workflow_repo.get(workflow_id)
        if workflow is None:
            raise OrchestrationError(f"workflow '{workflow_id}' not found")
        return workflow
