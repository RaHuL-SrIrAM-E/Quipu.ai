"""The deterministic verification policy — comparing post-deployment
production evidence against the original incident condition. No Gemini,
no LLM, no ADK anywhere in this module (see
docs/architecture/remediation_verification.md §13 for why): whether a
metric/log signal's already-computed severity indicates a problem, or
whether a latency value exceeds a configured threshold, is exactly the
kind of mechanical evidence comparison app/agents/monitoring.py's own
module docstring already establishes shouldn't go through an LLM
("do not use Gemini merely for mechanical API translation").

Deliberately small and closed — the signal types MonitoringAgent/Signal's
taxonomy already supports (app.domain.enums.SignalType), not a generic
anomaly-evaluation rules engine. A SignalType with no policy here simply
can't be verified — see docs/architecture/remediation_verification.md §9.
"""

from dataclasses import dataclass
from datetime import datetime

from app.agent_runtime.gateways.signals import SignalGateway
from app.config import get_settings
from app.domain import Signal, SignalSeverity, SignalType, VerificationOutcome
from app.persistence.repositories.signal import SignalQuery

# The only SignalTypes this policy knows how to read a health verdict
# from — a fixed, closed set matching exactly what MonitoringAgent's own
# adapters (app.signals.adapters) produce as operational evidence.
VERIFIABLE_SIGNAL_TYPES: frozenset[SignalType] = frozenset(
    {
        SignalType.METRIC_ANOMALY,
        SignalType.APPLICATION_ERROR,
        SignalType.LOG_ERROR,
        SignalType.AVAILABILITY_DEGRADATION,
        SignalType.LATENCY_ANOMALY,
    }
)

# Every type above except LATENCY_ANOMALY already carries a severity
# MonitoringAgent computed deterministically at collection time (see
# app.agents.monitoring._classify_error_rate and
# app.signals.adapters.normalize_cloud_logging_entry) — anything other
# than INFO means the condition is still present. LATENCY_ANOMALY is the
# one type MonitoringAgent deliberately leaves at SignalSeverity.INFO
# (its own module comment: "thresholding latency is a future policy
# addition... not implemented here") — verification is that addition,
# scoped narrowly to its own comparison, via a direct value/threshold
# check instead.
_SEVERITY_BASED_TYPES = VERIFIABLE_SIGNAL_TYPES - {SignalType.LATENCY_ANOMALY}

# MonitoringAgent emits these two unconditionally whenever there was any
# traffic/telemetry to observe at all (app.agents.monitoring._collect_metrics
# always creates a metric_anomaly/latency_anomaly Signal when Cloud
# Monitoring returned data, regardless of whether the value is healthy) —
# so their ABSENCE post-deployment genuinely means "we have no evidence,"
# never "it's healthy."
_ALWAYS_EMITTED_TYPES = frozenset({SignalType.METRIC_ANOMALY, SignalType.LATENCY_ANOMALY})

# LOG_ERROR/APPLICATION_ERROR (only created per matching ERROR+ log entry —
# app.agents.monitoring._collect_logs) and AVAILABILITY_DEGRADATION (only
# created when zero active instances are observed —
# app.agents.monitoring._collect_metrics) are the opposite: MonitoringAgent
# only ever emits them in the BAD case. Their absence post-deployment IS
# the healthy signal, not missing evidence — see
# docs/architecture/remediation_verification.md §9 "Presence-only
# conditions" for the documented reasoning and its limitation (this cannot
# distinguish "checked and found nothing" from "never checked").
_PRESENCE_ONLY_TYPES = VERIFIABLE_SIGNAL_TYPES - _ALWAYS_EMITTED_TYPES

ConditionVerdict = str  # "healthy" | "degraded" | "no_evidence"


@dataclass(frozen=True)
class ConditionEvaluation:
    signal_type: SignalType
    verdict: ConditionVerdict
    matched_signal_ids: list[str]


def evaluate_condition(signal_type: SignalType, matching_signals: list[Signal]) -> ConditionEvaluation:
    if not matching_signals:
        if signal_type in _PRESENCE_ONLY_TYPES:
            return ConditionEvaluation(signal_type=signal_type, verdict="healthy", matched_signal_ids=[])
        return ConditionEvaluation(signal_type=signal_type, verdict="no_evidence", matched_signal_ids=[])

    if signal_type in _SEVERITY_BASED_TYPES:
        degraded = any(s.severity != SignalSeverity.INFO for s in matching_signals)
    else:  # LATENCY_ANOMALY
        threshold = get_settings().verification_latency_p99_threshold_ms
        degraded = any(_numeric_value(s) > threshold for s in matching_signals)

    return ConditionEvaluation(
        signal_type=signal_type,
        verdict="degraded" if degraded else "healthy",
        matched_signal_ids=[s.signal_id for s in matching_signals],
    )


def _numeric_value(signal: Signal) -> float:
    value = signal.evidence.get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def collect_post_deployment_signals(
    signal_gateway: SignalGateway,
    *,
    condition_types: set[SignalType],
    service_name: str | None,
    environment: str | None,
    revision: str | None,
    since: datetime,
    until: datetime,
    max_signals: int,
) -> dict[SignalType, list[Signal]]:
    """Bounded, deterministic retrieval — one SignalQuery per condition
    type (same shape as app.detection.policy.count_related_signals /
    app.agents.detecting._retrieve_evidence), then filtered in
    application code by revision (§7 of the task: a stronger correlation
    field than timestamp alone, and SignalQuery itself has no revision
    filter — this is the "smallest additive" approach: no repository
    change, just a post-query filter). A signal that doesn't carry a
    revision at all is kept (best-effort — not every source populates
    it); a signal carrying a DIFFERENT revision than the one being
    verified is excluded."""
    results: dict[SignalType, list[Signal]] = {}
    for signal_type in condition_types:
        candidates = await signal_gateway.query(
            SignalQuery(
                signal_type=signal_type,
                service_name=service_name,
                environment=environment,
                since=since,
                until=until,
                limit=max_signals,
            )
        )
        results[signal_type] = [s for s in candidates if revision is None or s.revision is None or s.revision == revision]
    return results


def decide_outcome(
    condition_evaluations: list[ConditionEvaluation], *, total_post_deployment_signals: int, minimum_post_deployment_signals: int
) -> tuple[VerificationOutcome, str]:
    """The final deterministic decision (§8 of the task) — never zero
    evidence -> VERIFIED_RESOLVED, never missing data -> success."""
    if total_post_deployment_signals == 0:
        return VerificationOutcome.INSUFFICIENT_EVIDENCE, "no post-deployment signals were observed in the verification window"
    if total_post_deployment_signals < minimum_post_deployment_signals:
        return (
            VerificationOutcome.INSUFFICIENT_EVIDENCE,
            f"only {total_post_deployment_signals} post-deployment signal(s) observed, below the configured minimum of {minimum_post_deployment_signals}",
        )
    if not condition_evaluations:
        return VerificationOutcome.INSUFFICIENT_EVIDENCE, "the original incident's signal type(s) have no verification policy defined"

    degraded = [e.signal_type.value for e in condition_evaluations if e.verdict == "degraded"]
    if degraded:
        return VerificationOutcome.STILL_DEGRADED, f"post-deployment evidence still shows degraded condition(s): {', '.join(degraded)}"

    missing = [e.signal_type.value for e in condition_evaluations if e.verdict == "no_evidence"]
    if missing:
        return (
            VerificationOutcome.INSUFFICIENT_EVIDENCE,
            f"no post-deployment evidence for original condition(s): {', '.join(missing)} — cannot confirm recovery",
        )

    return VerificationOutcome.VERIFIED_RESOLVED, "all evaluable post-deployment condition(s) returned to a healthy state"
