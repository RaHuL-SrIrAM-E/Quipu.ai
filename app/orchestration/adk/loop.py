"""Bounded recovery loop: Codegen -> Testing -> evaluate, up to a configured
max_iterations. Used when a test failure is classified as a code defect —
repair the implementation and re-test, without recursively re-running the
whole workflow and without looping unboundedly.

Termination (via ADK's EventActions.escalate, which LoopAgent treats as
"stop iterating"):
  - tests pass                              -> stop (success)
  - failure isn't something Codegen can fix
    (architecture_defect / unknown)         -> stop (needs different routing)
  - max_iterations reached with no escalate -> ADK stops the loop on its own

The loop never decides the final workflow outcome itself — it only produces
evidence (the latest TestArtifact). app.orchestration.service inspects that
afterward and makes the authoritative call, same as the non-loop path.
"""

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import BaseAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.events.event_actions import EventActions

from app.agent_runtime.context import AgentContext
from app.agent_runtime.registry import AgentRegistry
from app.config import get_settings
from app.orchestration.adk.adapter import QuipuAgentAdkAdapter

_STOPPING_CLASSIFICATIONS = {"architecture_defect", "unknown"}


class _LoopEvaluator(BaseAgent):
    """Inspects the TestArtifact the previous adapter in this loop just
    produced and decides whether to keep looping."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        testing_output = state.get("testing_agent_output", {})
        artifacts: list[dict[str, Any]] = testing_output.get("artifacts", [])
        payload = artifacts[0]["payload"] if artifacts else {}
        overall_status = payload.get("overall_status")
        classifications = {f.get("classification") for f in payload.get("failures", [])}

        should_stop = overall_status == "passed" or bool(classifications & _STOPPING_CLASSIFICATIONS)

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(state_delta={"loop_should_stop": should_stop}, escalate=should_stop),
        )


def build_recovery_loop_agent(registry: AgentRegistry, context: AgentContext, max_iterations: int | None = None) -> LoopAgent:
    settings = get_settings()
    codegen_adapter = QuipuAgentAdkAdapter(
        name="codegen_agent",
        quipu_agent=registry.get("codegen_agent"),
        context=context,
        input_state_key="architecture_agent_artifact_id",
        output_state_key="codegen_agent_artifact_id",
    )
    testing_adapter = QuipuAgentAdkAdapter(
        name="testing_agent",
        quipu_agent=registry.get("testing_agent"),
        context=context,
        input_state_key="codegen_agent_artifact_id",
        output_state_key="testing_agent_artifact_id",
    )
    return LoopAgent(
        name="quipu_recovery_loop",
        sub_agents=[codegen_adapter, testing_adapter, _LoopEvaluator(name="loop_evaluator")],
        max_iterations=max_iterations or settings.orchestration_loop_max_iterations,
    )
