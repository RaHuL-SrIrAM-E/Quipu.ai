"""Feature Detection agent — deterministic, no LLM call.

Scans mocked customer signal data for feature-worthy patterns and emits a
single feature idea string into session state for Planning to consume.
Intentionally trivial for now: MOCK_CUSTOMER_SIGNALS and _detect_feature are
placeholders for real signal ingestion/analysis later.
"""

from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from google.genai import types

from app.core.db_hooks import stage_completed, stage_started
from app.core.observability import get_logger
from app.core.rbac import STAGE_ROLES, Permission

logger = get_logger("quipu.agent.feature_detection")

MOCK_CUSTOMER_SIGNALS = [
    {"source": "support_ticket", "text": "I keep losing my work because there's no way to save drafts."},
    {"source": "app_review", "text": "Love the app but I wish I could export my data to CSV."},
    {"source": "usage_log", "text": "user attempted export_csv action 42 times, all failed with 404"},
    {"source": "support_ticket", "text": "Please add dark mode, my eyes hurt using this at night."},
    {"source": "nps_survey", "text": "Would be nice to export reports for my manager."},
    {"source": "app_review", "text": "Dark mode when? Every other app has it already."},
]

_DEFAULT_FEATURE = "Add CSV export for reports and data tables"

_KEYWORD_FEATURE_MAP = {
    "export": "Add CSV export for reports and data tables",
    "csv": "Add CSV export for reports and data tables",
    "dark mode": "Add a dark mode theme toggle",
    "draft": "Add autosave/draft support so users don't lose work",
}


def _detect_feature(signals: list[dict]) -> str:
    counts: dict[str, int] = {}
    for signal in signals:
        text = signal["text"].lower()
        for keyword, feature in _KEYWORD_FEATURE_MAP.items():
            if keyword in text:
                counts[feature] = counts.get(feature, 0) + 1

    if not counts:
        return _DEFAULT_FEATURE
    return max(counts, key=counts.get)


def _rbac_gate(callback_context: CallbackContext) -> None:
    STAGE_ROLES["feature_detection"].requires(Permission.READ_KNOWLEDGE_BASE)


class FeatureDetectionAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        feature_idea = _detect_feature(MOCK_CUSTOMER_SIGNALS)
        logger.info("detected feature idea: %s", feature_idea)

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=feature_idea)]),
            actions=EventActions(state_delta={"feature_request": feature_idea}),
        )


feature_detection_agent = FeatureDetectionAgent(
    name="feature_detection",
    description="Detects a feature idea from mocked customer signal data.",
    before_agent_callback=[_rbac_gate, stage_started("feature_detection")],
    after_agent_callback=[stage_completed("feature_detection", "feature_request")],
)
