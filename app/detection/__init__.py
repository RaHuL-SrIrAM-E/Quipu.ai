"""Event-driven detection processing (see
docs/architecture/event_driven_detection.md).

    DetectionTrigger (app.eventing.trigger)
        -> DetectionProcessor (this package): resolves domain, evaluates
           the aggregation policy, invokes the EXISTING DetectingAgent
        -> DetectionResult, persisted through the existing
           DetectionGateway/DetectionRepository

This package is the only place (besides app.agents.detecting itself) that
constructs a DetectingAgent invocation — nothing here reimplements
evidence retrieval, reasoning, or dedup; all three remain DetectingAgent's
own responsibility.
"""

from app.detection.action_processor import ActionProcessingError, ActionProcessingOutcome, DetectionActionProcessor
from app.detection.action_trigger import ActionTrigger, DetectionAvailableEvent, NoOpActionTrigger
from app.detection.policy import AggregationPolicy, DomainPolicy, SIGNAL_TYPE_TO_DOMAIN
from app.detection.processor import DetectionProcessingError, DetectionProcessingOutcome, DetectionProcessor
from app.detection.trigger import DetectionProcessorTrigger

__all__ = [
    "ActionProcessingError",
    "ActionProcessingOutcome",
    "ActionTrigger",
    "AggregationPolicy",
    "DetectionActionProcessor",
    "DetectionAvailableEvent",
    "DetectionProcessingError",
    "DetectionProcessingOutcome",
    "DetectionProcessor",
    "DetectionProcessorTrigger",
    "DomainPolicy",
    "NoOpActionTrigger",
    "SIGNAL_TYPE_TO_DOMAIN",
]
