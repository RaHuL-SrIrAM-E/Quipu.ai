"""The API-level authorization boundary — deliberately small and honest.

This is NOT a reuse of AgentCapability as an HTTP-user permission system:
AgentCapability (app.agent_runtime.capabilities) governs what an AGENT may
do inside its own execution, checked by QuipuAgent/OrchestrationService
internals. A caller of this HTTP API is a different kind of principal
entirely (a human operator, or later a UI acting on their behalf) — this
module is the seam that maps an authenticated HTTP caller to the specific,
narrow capability set (`{AgentCapability.REVIEW_FEATURE_OPPORTUNITY}`)
FeatureReviewService's existing authorization already requires, without
pretending the two vocabularies are the same thing.

`Settings.api_auth_mode`:
  - "development": trusts the `X-Quipu-Reviewer-Id` request header as the
    caller's identity, for ATTRIBUTION only — never a privilege claim. No
    client-supplied field can grant capabilities (the granted set below is
    fixed, server-side, identical for every authenticated caller in this
    mode). Suitable for local development and this demo-era API only.
  - anything else ("disabled" or unset): every authenticated endpoint
    refuses with 401 — a real deployment must replace
    `require_reviewer_identity` with genuine token verification (e.g. a
    Google-signed identity token behind Cloud Run's own IAM, or an OIDC
    provider) before going live. That replacement is a change to this one
    function only — no route handler needs to change.

Never accepts a client-controlled privilege flag (e.g. `{"is_admin":
true}`) — see docs/architecture/control_plane_api.md "Authorization
boundary" for the full, honest statement of what this does and does not
protect against today.
"""

from dataclasses import dataclass, field

from fastapi import Header, HTTPException

from app.agent_runtime.capabilities import AgentCapability
from app.config import get_settings
from app.domain import DecisionSource

_DEVELOPMENT_GRANTED_CAPABILITIES = {AgentCapability.REVIEW_FEATURE_OPPORTUNITY}


@dataclass(frozen=True)
class ReviewerIdentity:
    reviewer_id: str
    reviewer_type: DecisionSource
    granted: frozenset[AgentCapability] = field(default_factory=frozenset)


async def require_reviewer_identity(
    x_quipu_reviewer_id: str | None = Header(default=None, alias="X-Quipu-Reviewer-Id"),
) -> ReviewerIdentity:
    settings = get_settings()
    if settings.api_auth_mode != "development":
        raise HTTPException(status_code=401, detail="authentication is not configured for this deployment")
    if not x_quipu_reviewer_id or not x_quipu_reviewer_id.strip():
        raise HTTPException(status_code=401, detail="X-Quipu-Reviewer-Id header is required")

    # reviewer_type is ALWAYS DecisionSource.HUMAN here, fixed server-side
    # — this endpoint category exists specifically to represent a human
    # acting through the control plane; it is never taken from the
    # request. See app/api/routes/feature_reviews.py and
    # app.feature_review.service.UnauthorizedReviewerError.
    return ReviewerIdentity(
        reviewer_id=x_quipu_reviewer_id.strip(),
        reviewer_type=DecisionSource.HUMAN,
        granted=frozenset(_DEVELOPMENT_GRANTED_CAPABILITIES),
    )
