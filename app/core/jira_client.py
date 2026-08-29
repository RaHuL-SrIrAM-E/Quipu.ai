"""Thin Jira Cloud REST v3 client — API-token auth, no MCP/OAuth, so it works headlessly from a pipeline."""

import requests

from app.config import get_settings


class JiraConfigError(Exception):
    pass


def is_transient_jira_error(exc: Exception) -> bool:
    """The failure classifier the resilience layer's retry/circuit
    breaker use around this client (see
    app.feature_review.service.FeatureReviewService.approve and
    docs/architecture/resilience.md "Jira"). A 5xx response or a network-
    level failure (connection refused, timeout) is transient — Jira's own
    infrastructure is temporarily unavailable, safe to retry. A 4xx
    response (bad auth, invalid project key, malformed request) is
    permanent — retrying an identical request would just fail identically
    every time, so it must never be retried and must never count toward
    tripping the circuit breaker.

    Also unwraps app.core.resilience.retry.RetryExhaustedError (raised
    once retry_async's own attempts are used up) to classify by the
    *original* underlying error — so the circuit breaker, which observes
    one outcome per retry_async() call rather than per individual HTTP
    attempt, still counts "Jira was down for a whole retry sequence" as
    the transient failure it actually is."""
    from app.core.resilience.retry import RetryExhaustedError

    if isinstance(exc, RetryExhaustedError):
        return is_transient_jira_error(exc.last_error)
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    return False


class JiraClient:
    def __init__(self):
        settings = get_settings()
        if not (settings.jira_base_url and settings.jira_email and settings.jira_api_token and settings.jira_project_key):
            raise JiraConfigError(
                "JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN and JIRA_PROJECT_KEY must all be set"
            )
        self.base_url = settings.jira_base_url.rstrip("/")
        self.project_key = settings.jira_project_key
        self.auth = (settings.jira_email, settings.jira_api_token)

    def create_story(self, summary: str, description: str) -> dict:
        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                    ],
                },
                "issuetype": {"name": "Story"},
            }
        }
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue",
            json=payload,
            auth=self.auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return {"key": data["key"], "url": f"{self.base_url}/browse/{data['key']}"}
