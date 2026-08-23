from typing import Any

from app.agents.base import BaseAgent
from app.core.rbac import Permission


class TestingAgent(BaseAgent):
    stage_name = "testing"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.check_permission(Permission.RUN_TESTS)
        # TODO: generate/run tests against the coding stage's diff
        return {"test_results": None}
