from typing import Any

from app.agents.base import BaseAgent
from app.core.rbac import Permission


class DevOpsAgent(BaseAgent):
    stage_name = "devops"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.check_permission(Permission.DEPLOY)
        # TODO: build/deploy the tested change
        return {"deployment": None}
