"""The deterministic aggregation policy DetectionProcessor uses to decide
*what to ask DetectingAgent* and *whether to ask it at all* — never how to
interpret evidence (that stays entirely inside DetectingAgent/Gemini).

Not a generic streaming/windowing framework: a fixed, per-DetectionDomain
policy (window, max signals, minimum related-signal count) plus a thin
count-only query used purely as a pre-invocation gate. All actual evidence
retrieval DetectingAgent performs for the real detection pass is still
DetectingAgent's own existing `_retrieve_evidence` — this module does not
re-implement it, it only decides whether that retrieval is worth doing.

Signal <-> DetectionDomain mapping is imported from app.agents.detecting
(OPERATIONAL_SIGNAL_TYPES/PRODUCT_SIGNAL_TYPES/DOMAIN_SIGNAL_TYPES) rather
than redefined here — one taxonomy, reused, not duplicated.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.agent_runtime.gateways.signals import SignalGateway
from app.agents.detecting import DOMAIN_SIGNAL_TYPES
from app.config import get_settings
from app.domain import DetectionDomain, SignalType
from app.persistence.repositories.signal import SignalQuery

# Reverse of DOMAIN_SIGNAL_TYPES — which DetectionDomain a given SignalType
# belongs to. A SignalType not present here has no detection domain (yet)
# and is never a valid trigger for detection processing.
SIGNAL_TYPE_TO_DOMAIN: dict[SignalType, DetectionDomain] = {
    signal_type: domain for domain, signal_types in DOMAIN_SIGNAL_TYPES.items() for signal_type in signal_types
}


@dataclass(frozen=True)
class DomainPolicy:
    window_minutes: int
    min_related_signals: int


@dataclass(frozen=True)
class AggregationPolicy:
    """Bounded, explicit, per-domain — not a general routing DSL. `for_domain`
    is the only thing DetectionProcessor calls; everything else is
    construction-time configuration, sourced from app.config.Settings so
    it's overridable the same way every other Quipu ceiling is, without a
    second configuration mechanism."""

    operational: DomainPolicy
    product: DomainPolicy

    @classmethod
    def from_settings(cls) -> "AggregationPolicy":
        settings = get_settings()
        return cls(
            operational=DomainPolicy(
                window_minutes=settings.detection_operational_window_minutes,
                min_related_signals=settings.detection_min_operational_signals,
            ),
            product=DomainPolicy(
                window_minutes=settings.detection_product_window_minutes,
                min_related_signals=settings.detection_min_product_signals,
            ),
        )

    def for_domain(self, domain: DetectionDomain) -> DomainPolicy:
        return self.operational if domain == DetectionDomain.OPERATIONAL else self.product


async def count_related_signals(
    signal_gateway: SignalGateway,
    *,
    domain: DetectionDomain,
    service_name: str | None,
    environment: str | None,
    window_minutes: int,
    max_signals: int,
) -> int:
    """A cheap, bounded, deterministic pre-check: how many Signals exist in
    this domain's default SignalType set, within the window and
    service/environment scope, without retrieving/ranking/deduplicating
    them the way DetectingAgent's real evidence pass does. Used only to
    decide whether invoking DetectingAgent (and therefore Gemini) is
    warranted at all — see DetectionProcessor.process_signal_available.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    signal_ids: set[str] = set()
    for signal_type in DOMAIN_SIGNAL_TYPES[domain]:
        results = await signal_gateway.query(
            SignalQuery(
                signal_type=signal_type,
                service_name=service_name,
                environment=environment,
                since=since,
                limit=max_signals,
            )
        )
        signal_ids.update(s.signal_id for s in results)
        if len(signal_ids) >= max_signals:
            break
    return len(signal_ids)
