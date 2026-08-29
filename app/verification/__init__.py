"""Post-remediation production verification (see
docs/architecture/remediation_verification.md).

Deployment success is never treated as incident resolution — only a
RemediationVerification with outcome VERIFIED_RESOLVED (produced by
RemediationVerificationService, comparing real post-deployment production
Signals against the original incident condition) represents Quipu having
actually checked. No LLM/agent anywhere in this package.
"""

from app.verification.errors import VerificationError
from app.verification.service import RemediationVerificationService

__all__ = ["RemediationVerificationService", "VerificationError"]
