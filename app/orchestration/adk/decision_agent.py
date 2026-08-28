"""The orchestration-level ADK LlmAgent — used only for the cases the
deterministic classification table (app.orchestration.decisions) can't
resolve on its own: mixed failure classifications, dependency failures, or
anything else ambiguous enough to need real reasoning.

No tools. It cannot invoke agents, read the repository, or query knowledge —
it only ever sees the WorkflowEvidence it's handed and returns a
ProposedDecision. It never deploys, modifies code, or executes anything
itself; app.orchestration.service validates and executes.
"""

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import ValidationError

from app.config import get_settings
from app.core.observability import get_logger
from app.domain import DecisionAction
from app.orchestration.decisions import ProposedDecision, WorkflowEvidence

settings = get_settings()
logger = get_logger("quipu.orchestration.decision_agent")

_INSTRUCTION = """You are Quipu's orchestration decision engine.

You do not execute agents. You do not modify repositories. You do not deploy.

You are given structured workflow evidence — the current stage, the test
status, failure classifications from the Testing stage, and the retry
budget already spent. Analyze ONLY that evidence and recommend the next
valid workflow action.

Respect the retry budget: if retry_count has reached max_retries, do not
recommend retrying — recommend escalate instead.

Valid actions: continue (proceed to the next stage), retry (target_agent
must be the stage that should re-run), replan (target_agent must be
architecture_agent), escalate (hand this to a human), complete, fail.

Do not invent evidence you weren't given. If the evidence is genuinely
ambiguous, prefer escalate over guessing.

Return only the structured decision: action, target_agent (if applicable),
reason, confidence."""

decision_agent = LlmAgent(
    name="orchestration_decision",
    description="Recommends the next workflow action from structured evidence. Never executes anything itself.",
    model=settings.gemini_model,
    instruction=_INSTRUCTION,
    output_schema=ProposedDecision,
    output_key="decision",
)


async def propose_decision(evidence: WorkflowEvidence, runner_cls: type = InMemoryRunner) -> ProposedDecision:
    """Runs the orchestration decision agent once and returns its
    recommendation. This is the ONLY function in app.orchestration.service
    that touches ADK directly — runner_cls is injectable so tests can supply
    a fake without any real Gemini call. A missing/invalid response degrades
    to ESCALATE (never a fabricated CONTINUE) — consistent with the
    evidence-first posture established for TestingAgent."""
    runner = runner_cls(agent=decision_agent, app_name="quipu")
    session = await runner.session_service.create_session(app_name="quipu", user_id=evidence.workflow_id, state={})
    message = types.Content(role="user", parts=[types.Part(text=evidence.model_dump_json())])

    final_text = ""
    try:
        async for event in runner.run_async(user_id=evidence.workflow_id, session_id=session.id, new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = event.content.parts[0].text
    except Exception:
        logger.exception("orchestration decision agent execution failed")
        return ProposedDecision(action=DecisionAction.ESCALATE, reason="orchestration decision agent execution failed", confidence=0.0)

    if not final_text.strip():
        return ProposedDecision(action=DecisionAction.ESCALATE, reason="orchestration decision agent returned no output", confidence=0.0)

    try:
        return ProposedDecision.model_validate_json(final_text)
    except ValidationError:
        return ProposedDecision(
            action=DecisionAction.ESCALATE, reason="orchestration decision agent returned invalid output", confidence=0.0
        )
