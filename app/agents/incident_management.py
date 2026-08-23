from typing import Any

from app.agents.base import BaseAgent
from app.core.rbac import Permission


class IncidentManagementAgent(BaseAgent):
    stage_name = "incident_management"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.check_permission(Permission.READ_MONITORING)
        # TODO: triage/escalate if monitoring flags a regression
        return {"incident": None}
