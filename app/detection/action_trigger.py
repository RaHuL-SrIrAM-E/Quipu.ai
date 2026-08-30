"""The action trigger boundary — the DetectionResult -> Action equivalent
of app.eventing.trigger.DetectionTrigger. DetectionProcessor must not call
FeatureReviewService or IncidentResolutionAgent directly — it invokes this
narrow interface after a DetectionResult is durably persisted, so
"interpret evidence" (DetectionProcessor/DetectingAgent) stays separate
from "decide what to do about an actionable interpretation"
(DetectionActionProcessor).

DetectionAvailableEvent (not the full DetectionResult) is what crosses
this boundary — detection_id plus the one field (detection_type) needed to
route without a second fetch. Deliberately narrow, same rationale as
app.eventing.trigger.SignalAvailableEvent: nothing downstream of this
Protocol needs to reason about DetectionResult's other fields directly:
DetectionActionProcessor re-fetches the full record itself via
DetectionGateway when it needs more.

NoOpActionTrigger is a safe default: it records that a DetectionResult
became available and does nothing else. app.detection.action_processor
provides the real implementation (DetectionActionProcessor), which is
where FeatureReviewService/IncidentResolutionAgent actually get invoked —
never in this module.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.observability import get_logger
from app.domain import DetectionType

logger = get_logger("quipu.detection.action_trigger")


@dataclass(frozen=True)
class DetectionAvailableEvent:
    """A stable reference to a just-persisted DetectionResult — never the
    full record. Built by DetectionProcessor from the DetectionResult it
    just resolved (freshly created or matched via DetectingAgent's own
    fingerprint dedup — either way, already persisted)."""

    detection_id: str
    detection_type: DetectionType


@runtime_checkable
class ActionTrigger(Protocol):
    async def on_detection_available(self, event: DetectionAvailableEvent) -> None: ...


class NoOpActionTrigger:
    """Logs that a DetectionResult became available for action; invokes no
    processor. A failure here must never be allowed to affect detection
    processing's own success — see DetectionProcessor, which persists the
    DetectionResult before ever calling the trigger."""

    async def on_detection_available(self, event: DetectionAvailableEvent) -> None:
        logger.info("detection.available_for_action detection_id=%s detection_type=%s", event.detection_id, event.detection_type.value)
