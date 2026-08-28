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

from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.artifacts import RepositoryArtifactGateway
from app.agent_runtime.gateways.knowledge import KnowledgeGateway
from app.agent_runtime.gateways.tools import ToolGateway
from app.agent_runtime.registry import AgentNotFoundError, AgentRegistry
from app.core.observability import get_logger
from app.domain import (
    AgentInput,
    ArtifactType,
    Decision,
    DecisionAction,
    DecisionSource,
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
    deterministic_action,
)
from app.orchestration.errors import (
    InvalidTransitionError,
    OrchestrationError,
    RetryLimitExceededError,
    UnknownAgentError,
)
from app.orchestration.transitions import (
    STAGE_TO_AGENT_ID,
    STAGE_TO_ARTIFACT_TYPE,
    can_transition,
    next_stage,
)
from app.persistence.repositories.artifact import ArtifactRepository
from app.persistence.repositories.decision import DecisionRepository
from app.persistence.repositories.execution import AgentExecutionRepository
from app.persistence.repositories.workflow import WorkflowRepository

logger = get_logger("quipu.orchestration.service")

_TERMINAL_STATUSES = {WorkflowStatus.COMPLETED, WorkflowStatus.ESCALATED, WorkflowStatus.CANCELLED, WorkflowStatus.FAILED}


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

    # ---- public API -----------------------------------------------------

    async def start_workflow(self, ticket: Ticket, *, workspace_path: str | None = None) -> WorkflowState:
        workflow = WorkflowState(
            ticket=ticket,
            status=WorkflowStatus.PENDING,
            current_stage=WorkflowStage.PLANNING,
            metadata={"workspace_path": workspace_path} if workspace_path else {},
        )
        await self._workflow_repo.create(workflow)
        logger.info("workflow %s started for ticket '%s'", workflow.workflow_id, ticket.title)
        return workflow

    async def execute_next_step(self, workflow_id: str) -> WorkflowState:
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

        input_artifact_id = workflow.artifact_ids[-1] if workflow.artifact_ids else None
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

    async def _execute_decision(self, workflow: WorkflowState, decision: Decision) -> WorkflowState:
        if decision.action == DecisionAction.CONTINUE:
            nxt = next_stage(workflow.current_stage)
            update = (
                {"status": WorkflowStatus.PENDING, "current_stage": nxt}
                if nxt
                else {"status": WorkflowStatus.COMPLETED, "current_stage": WorkflowStage.COMPLETED}
            )
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
