"""Controlled repository mutation tool for Codegen — the ONLY way any Quipu
agent may write to the filesystem. No shell access, ever.

Reuses repo_tools' _workspace/_safe_join directly rather than reimplementing
path safety — that logic is security-critical and must not drift into a
second implementation.

Enforces, in order:
1. WRITE_CODE capability granted (state["_capabilities"])
2. path is not absolute
3. path is within the architecture-approved scope (state["_allowed_paths"])
4. resolved path does not escape the repo workspace (traversal / symlink escape)

Returns a result dict rather than raising on rejection, so the model sees
the failure and can react (e.g. note an out-of-scope need in
unresolved_items) instead of the whole turn crashing. A rejected write never
touches disk. CodegenAgent additionally verifies the real filesystem state
after the run — this tool's self-reported "success" is not the final source
of truth for what actually changed.
"""

from pathlib import Path

from google.adk.tools import ToolContext

from app.agent_runtime.capabilities import AgentCapability
from app.tools.repo_tools import _safe_join, _workspace


def write_file(path: str, content: str, tool_context: ToolContext) -> dict:
    """Write content to a file within the architecture-approved scope for the
    task being implemented. Refuses (does not silently expand scope) if path
    is not one of the files Architecture specified. Creates parent
    directories as needed; overwrites if the file already exists.
    """
    granted: set[AgentCapability] = tool_context.state.get("_capabilities", set())
    if AgentCapability.WRITE_CODE not in granted:
        return {"success": False, "path": path, "error": "WRITE_CODE capability not granted"}

    if Path(path).is_absolute():
        return {"success": False, "path": path, "error": "absolute paths are not allowed"}

    normalized = path.lstrip("./")
    allowed = set(tool_context.state.get("_allowed_paths", []))
    if normalized not in allowed:
        return {
            "success": False,
            "path": path,
            "error": f"'{path}' is outside the architecture-approved scope for this task",
        }

    root = _workspace(tool_context)
    try:
        target = _safe_join(root, normalized)
    except ValueError as exc:
        return {"success": False, "path": path, "error": str(exc)}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"success": True, "path": normalized}


CODEGEN_TOOLS = [write_file]
