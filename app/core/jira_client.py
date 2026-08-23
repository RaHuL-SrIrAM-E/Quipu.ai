"""Thin Jira Cloud REST v3 client — API-token auth, no MCP/OAuth, so it works headlessly from a pipeline."""

import requests

from app.config import get_settings


class JiraConfigError(Exception):
    pass


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
