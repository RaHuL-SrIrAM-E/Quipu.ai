"""Builds a real ADK SequentialAgent representing the Level 2.0 happy path:
Planning -> Architecture -> Codegen -> Testing.

This is one of two execution mechanisms app.orchestration.service can use.
The step-wise path (OrchestrationService.execute_next_step, one stage at a
time with a durable Firestore write after each) is the crash-safe,
production-recommended default. Running this whole SequentialAgent in one
ADK session is available for a single synchronous call but loses per-stage
resumability if the process dies mid-sequence — see
docs/architecture/orchestration.md "SequentialAgent vs. step-wise execution."
"""

from google.adk.agents import SequentialAgent

from app.agent_runtime.context import AgentContext
from app.agent_runtime.registry import AgentRegistry
from app.orchestration.adk.adapter import QuipuAgentAdkAdapter
from app.orchestration.transitions import STAGE_ORDER, STAGE_TO_AGENT_ID


def build_happy_path_sequential_agent(registry: AgentRegistry, context: AgentContext) -> SequentialAgent:
    sub_agents = []
    previous_output_key: str | None = None

    for stage in STAGE_ORDER:
        agent_id = STAGE_TO_AGENT_ID[stage]
        output_key = f"{agent_id}_artifact_id"
        sub_agents.append(
            QuipuAgentAdkAdapter(
                name=agent_id,
                quipu_agent=registry.get(agent_id),
                context=context,
                input_state_key=previous_output_key,
                output_state_key=output_key,
            )
        )
        previous_output_key = output_key

    return SequentialAgent(name="quipu_happy_path", sub_agents=sub_agents)
