"""QuipuAgentAdkAdapter — wraps one QuipuAgent so it can be a sub_agent
inside a real ADK SequentialAgent/LoopAgent.

ADK owns sequencing/looping; the QuipuAgent still owns 100% of the business
logic (reasoning, validation, capability enforcement, artifact persistence)
— this adapter only translates between ADK's InvocationContext and Quipu's
AgentInput/AgentContext/AgentOutput, and relays the produced artifact id
through ADK session state so the *next* adapter in the sequence can build
its own AgentInput from it.

Per the Level 2.0 architectural boundary: ADK session state here is
execution context ONLY, scoped to one in-process run. Durable, authoritative
workflow-state advancement (current_stage, artifact_ids, version) happens
separately, in app.orchestration.service, via Quipu's own WorkflowRepository
— never through this adapter or ADK session state. If the process crashes
mid-SequentialAgent-run, this session state is simply lost; recovery relies
entirely on what app.orchestration.service already persisted per completed
stage (see docs/architecture/orchestration.md "Crash recovery").
"""

import uuid
from collections.abc import AsyncGenerator
from dataclasses import replace as dataclass_replace
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from google.genai import types

from app.domain import AgentInput, Ticket, WorkflowStatus


class QuipuAgentAdkAdapter(BaseAgent):
    quipu_agent: Any  # QuipuAgent — Any to avoid pydantic trying to schema-ize an ABC/dataclass
    context: Any  # AgentContext, shared gateways; execution_id is replaced per invocation below
    input_state_key: str | None = None  # session state key holding this stage's input artifact id (None for the first stage)
    output_state_key: str = ""  # session state key this stage's output artifact id is written to, for the next adapter

    model_config = {"arbitrary_types_allowed": True}

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        ticket = Ticket.model_validate(state["ticket"])

        artifact_ids: list[str] = []
        if self.input_state_key:
            input_artifact_id = state.get(self.input_state_key)
            if input_artifact_id:
                artifact_ids = [input_artifact_id]

        agent_input = AgentInput(
            workflow_id=state["workflow_id"],
            agent_name=self.quipu_agent.identity.agent_id,
            ticket=ticket,
            artifact_ids=artifact_ids,
            context={"workspace_path": state["workspace_path"]} if state.get("workspace_path") else {},
        )

        invocation_context = dataclass_replace(self.context, execution_id=str(uuid.uuid4()))
        output = await self.quipu_agent.execute(agent_input, invocation_context)

        state_delta: dict[str, Any] = {
            f"{self.name}_status": output.status.value,
            f"{self.name}_output": output.model_dump(mode="json"),
        }
        if output.artifacts and self.output_state_key:
            state_delta[self.output_state_key] = output.artifacts[0].artifact_id

        message = output.messages[0] if output.messages else (output.errors[0].message if output.errors else "")

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=message)]),
            actions=EventActions(
                state_delta=state_delta,
                # Stop the SequentialAgent/LoopAgent early on anything but a
                # clean completion — no point running Architecture if
                # Planning failed. The orchestrator makes its own, separate,
                # authoritative pass/fail determination regardless of this.
                escalate=output.status != WorkflowStatus.COMPLETED,
            ),
        )
