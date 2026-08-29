"""VerificationError — raised when verify_remediation() is called against
state that makes verification meaningless (wrong resolution_id, no
associated INCIDENT DetectionResult, no deployment to correlate against).
Distinct from VerificationOutcome.ESCALATED (app.domain.
remediation_verification): an error means "you asked the wrong question";
ESCALATED means "the question was valid, but what we found needs a
human" — see docs/architecture/remediation_verification.md "Failure
semantics".
"""


class VerificationError(Exception):
    pass
