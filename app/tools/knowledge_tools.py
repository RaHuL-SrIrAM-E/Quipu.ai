"""Enterprise knowledge tool for agents — on-demand, not auto-injected into
every prompt. The model decides when a query would help and calls this
itself; nothing stuffs a large knowledge context into the instruction.

Reads a KnowledgeGateway out of ADK session state (state["_knowledge_gateway"],
seeded by whoever runs the agent — see PlanningAgent/ArchitectureAgent
._perform) rather than constructing one itself: agents never instantiate a
knowledge/search client directly. Shared by every agent (state["_agent_name"]
picks the retrieval profile — see app.knowledge.policies.AGENT_RETRIEVAL_PROFILES
— so this isn't duplicated per agent). Returns a list of plain dicts mirroring
KnowledgeItem's fields (document_id, source, relevance_score, etc.) so
provenance survives the round trip through the model.
"""

from google.adk.tools import ToolContext

from app.agent_runtime.gateways.knowledge import KnowledgeGateway
from app.domain import KnowledgeRequest, KnowledgeType


async def query_enterprise_knowledge(query: str, knowledge_type: str, tool_context: ToolContext) -> list[dict]:
    """Search enterprise knowledge — architecture patterns, compliance rules,
    technology standards, security/platform requirements, or historical
    project context — relevant to the current task. Only the knowledge_type
    values in this agent's retrieval profile are allowed. Use only when it
    would materially improve the result; don't call this for every task, and
    never fabricate an enterprise standard if nothing relevant is found.
    """
    gateway: KnowledgeGateway | None = tool_context.state.get("_knowledge_gateway")
    if gateway is None:
        return []  # no knowledge backend configured for this run — safe no-op, not a failure

    from app.knowledge.policies import get_retrieval_policy

    agent_name = tool_context.state.get("_agent_name", "planning_agent")
    policy = get_retrieval_policy(agent_name)

    try:
        parsed_type = KnowledgeType(knowledge_type)
    except ValueError as exc:
        raise ValueError(f"invalid knowledge_type '{knowledge_type}'") from exc

    if parsed_type not in policy.allowed_knowledge_types:
        allowed = ", ".join(t.value for t in policy.allowed_knowledge_types)
        raise ValueError(f"knowledge_type '{knowledge_type}' is outside the {agent_name} profile ({allowed})")

    request = KnowledgeRequest(
        agent_name=agent_name,
        workflow_id=tool_context.state.get("workflow_id", ""),
        query=query,
        knowledge_type=parsed_type,
        top_k=policy.default_top_k,
    )
    items = await gateway.search(request)
    return [item.model_dump(mode="json") for item in items]


KNOWLEDGE_TOOLS = [query_enterprise_knowledge]
