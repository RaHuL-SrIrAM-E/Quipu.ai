from typing import Any

from app.agents.base import BaseAgent
from app.core.rbac import Permission


class ArchitectureAgent(BaseAgent):
    stage_name = "architecture"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.check_permission(Permission.READ_CODEBASE)
        # TODO: propose architecture/design changes for the plan
        return {"design": None}
