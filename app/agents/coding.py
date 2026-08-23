from typing import Any

from app.agents.base import BaseAgent
from app.core.rbac import Permission


class CodingAgent(BaseAgent):
    stage_name = "coding"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.check_permission(Permission.WRITE_CODE)
        # TODO: generate code changes implementing the design
        return {"diff": None}
