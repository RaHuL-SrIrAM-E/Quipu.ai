from typing import Any

from app.agents.base import BaseAgent
from app.core.rbac import Permission


class MonitoringAgent(BaseAgent):
    stage_name = "monitoring"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.check_permission(Permission.READ_MONITORING)
        # TODO: watch post-deploy signals for the change
        return {"health": None}
