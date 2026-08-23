from typing import Any

from app.agents.base import BaseAgent
from app.core.rbac import Permission


class FeatureDetectionAgent(BaseAgent):
    stage_name = "feature_detection"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.check_permission(Permission.READ_KNOWLEDGE_BASE)
        # TODO: classify feature_request against the knowledge base (re-ranker retrieval)
        return {"detected_feature": state["feature_request"], "confidence": None}
