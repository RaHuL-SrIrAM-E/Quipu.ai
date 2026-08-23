"""Jira tool for agents — creates a Story via Jira Cloud REST API (no MCP/OAuth needed)."""

from google.adk.tools import ToolContext

from app.core.jira_client import JiraClient
from app.core.rbac import STAGE_ROLES, Permission


def create_story(summary: str, description: str, tool_context: ToolContext) -> dict:
    """Create a Jira Story for one task. Returns the created issue's key and URL."""
    STAGE_ROLES["planning"].requires(Permission.WRITE_JIRA)
    client = JiraClient()
    return client.create_story(summary=summary, description=description)


JIRA_TOOLS = [create_story]
