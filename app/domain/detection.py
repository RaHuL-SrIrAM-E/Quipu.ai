"""DetectionResult — Detecting's interpretation of a bounded set of Signals.
Framework-independent (no Google SDK imports here — DetectingAgent's Gemini/
ADK integration lives in app/agents/detecting.py).

This is deliberately NOT a Signal (evidence is never mutated — see
app.domain.signal.Signal's own docstring) and NOT yet an
IncidentCandidate/FeatureCandidate (those are future work, explicitly out
of scope for Level 3.2):

    Signal          = "what was observed"                  (app.domain.signal)
    DetectionResult = "what Quipu's interpretation layer believes
                       a bounded set of Signals may represent"  (this module)
    Candidate       = "a reviewable IncidentCandidate/FeatureCandidate"  (future)
    Ticket          = "what the organization decided to act upon"  (existing)

Not persisted as an Artifact: Artifact's `parent_artifact_ids` lineage
models an SDLC stage-to-stage handoff (Plan -> Architecture -> Code -> Test
-> Deploy), and DetectionResult isn't consumed by another QuipuAgent in
that chain in this level — forcing it into Artifact would blur "an agent's
completed unit of SDLC work" with "an interpretation of observed evidence,"
exactly the distinction the Signal/Artifact split (Level 3) already
protects. Instead it gets the same treatment Signal did: its own narrow
domain model + repository, following the identical pattern rather than
inventing a new one.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from app.domain.enums import DetectionDomain, DetectionType, SignalSeverity


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value


def compute_detection_fingerprint(
    *, detection_type: DetectionType, subject: str, supporting_signal_ids: list[str], window_minutes: int
) -> str:
    """Detection's own identity/dedup boundary (Level 3.2 §21) — a
    deliberately separate function from app.domain.signal.compute_fingerprint,
    not a reuse of it: Signal identity is about a single observed event;
    detection identity is about one interpretation of a set of signals
    within one observation window. Conflating the two would blur exactly
    the evidence/interpretation distinction this whole level protects.
    Same signal set + type + subject + window -> same fingerprint, so
    running Detecting again over unchanged evidence doesn't create an
    uncontrolled duplicate DetectionResult.
    """
    basis = "|".join([detection_type.value, subject.strip().lower(), ",".join(sorted(supporting_signal_ids)), str(window_minutes)])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class DetectionResult(BaseModel):
    detection_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    detection_type: DetectionType
    domain: DetectionDomain

    title: str
    summary: str
    rationale: str  # concise decision rationale — NEVER hidden chain-of-thought, see module docstring

    confidence: float = Field(ge=0.0, le=1.0)  # confidence in the INTERPRETATION, not a measure of signal quality
    severity: SignalSeverity | None = None  # reuses Signal's severity vocabulary rather than inventing a second one

    subject: str  # affected service/product area
    service_name: str | None = None
    environment: str | None = None

    # Evidence-first (§10): every id here is verified, before this object is
    # ever constructed, to be a signal_id that was actually part of the
    # retrieved evidence set — see app.agents.detecting._validate_evidence.
    # This model does not re-validate that itself (it has no repository
    # access), it only carries the already-validated result.
    supporting_signal_ids: list[str] = Field(default_factory=list)

    # Contextual grounding, NOT evidence (§14) — KnowledgeItem.document_id
    # values the model reported consulting. Unlike supporting_signal_ids,
    # these are not cross-checked against a retrieval log (Enterprise
    # Knowledge informs interpretation; it is never treated as a production
    # event), so this list is best-effort provenance, not a hard guarantee.
    knowledge_references: list[str] = Field(default_factory=list)

    observation_window_minutes: int = Field(gt=0)
    detected_at: datetime = Field(default_factory=_utc_now)
    created_by: str = "detecting_agent"
    fingerprint: str

    _validate_detected_at = field_validator("detected_at")(_require_aware)

    @field_validator("title", "summary", "rationale", "subject", "fingerprint")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
