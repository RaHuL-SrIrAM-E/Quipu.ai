"""Base class every pipeline-stage agent extends.

Centralizes the cross-cutting concerns (RBAC enforcement, tracing, cost/latency
capture) so an individual agent only has to implement `run`.
"""

from abc import ABC, abstractmethod
from typing import Any

from app.core.llm import GeminiClient
from app.core.metrics import RunMetrics
from app.core.observability import get_logger, span
from app.core.rbac import STAGE_ROLES, AgentRole, Permission


class BaseAgent(ABC):
    stage_name: str

    def __init__(self, llm: GeminiClient | None = None):
        self.llm = llm or GeminiClient()
        self.logger = get_logger(f"quipu.agent.{self.stage_name}")
        self.role: AgentRole = STAGE_ROLES[self.stage_name]

    def check_permission(self, permission: Permission) -> None:
        self.role.requires(permission)

    @abstractmethod
    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """Execute this stage. Return the partial state update to merge in."""

    def __call__(self, state: dict[str, Any], metrics: RunMetrics | None = None) -> dict[str, Any]:
        with span(f"stage.{self.stage_name}", run_id=state.get("run_id")) as record:
            try:
                result = self.run(state)
            except Exception as exc:
                self.logger.exception("stage '%s' failed", self.stage_name)
                return {
                    "current_stage": self.stage_name,
                    "errors": [f"{self.stage_name}: {exc}"],
                }
            record["status"] = "ok"

        if metrics is not None:
            cost = result.pop("_cost_usd", 0.0)
            latency = result.pop("_latency_ms", 0.0)
            metrics.record(self.stage_name, cost_usd=cost, latency_ms=latency)

        return {
            "current_stage": self.stage_name,
            "stage_outputs": {self.stage_name: result},
        }
