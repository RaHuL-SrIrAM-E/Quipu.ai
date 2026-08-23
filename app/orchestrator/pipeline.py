"""Sequential pipeline orchestrator, built from native ADK sub-agents.

Only Planning is wired in so far. Later stages get appended to sub_agents as
they're rebuilt as native ADK agents (see app/agents/planning.py for the pattern).
The Coding<->Testing retry loop, when built, should be a LoopAgent nested in here.
"""

from google.adk.agents import SequentialAgent

from app.agents.architecture import architecture_agent
from app.agents.planning import planning_agent

quipu_pipeline = SequentialAgent(
    name="quipu_pipeline",
    description="Runs Quipu's SDLC stages in order.",
    sub_agents=[planning_agent, architecture_agent],
)
