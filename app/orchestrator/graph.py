"""Wires the 8 pipeline stages into a single sequential LangGraph graph."""

from langgraph.graph import END, StateGraph

from app.agents.architecture import ArchitectureAgent
from app.agents.coding import CodingAgent
from app.agents.devops import DevOpsAgent
from app.agents.feature_detection import FeatureDetectionAgent
from app.agents.incident_management import IncidentManagementAgent
from app.agents.monitoring import MonitoringAgent
from app.agents.planning import PlanningAgent
from app.agents.testing import TestingAgent
from app.core.metrics import RunMetrics
from app.orchestrator.state import PipelineState

STAGE_ORDER = [
    "feature_detection",
    "planning",
    "architecture",
    "coding",
    "testing",
    "devops",
    "monitoring",
    "incident_management",
]

_AGENT_CLASSES = {
    "feature_detection": FeatureDetectionAgent,
    "planning": PlanningAgent,
    "architecture": ArchitectureAgent,
    "coding": CodingAgent,
    "testing": TestingAgent,
    "devops": DevOpsAgent,
    "monitoring": MonitoringAgent,
    "incident_management": IncidentManagementAgent,
}


def build_graph(metrics: RunMetrics | None = None):
    graph = StateGraph(PipelineState)
    metrics = metrics or RunMetrics()

    for stage_name in STAGE_ORDER:
        agent = _AGENT_CLASSES[stage_name]()
        graph.add_node(stage_name, lambda state, agent=agent: agent(state, metrics=metrics))

    graph.set_entry_point(STAGE_ORDER[0])
    for current, nxt in zip(STAGE_ORDER, STAGE_ORDER[1:]):
        graph.add_edge(current, nxt)
    graph.add_edge(STAGE_ORDER[-1], END)

    return graph.compile()
