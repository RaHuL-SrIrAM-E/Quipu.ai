"""Repo-aware tools agents use to ground themselves in the actual checked-out codebase.

Each tool reads `workspace_path` out of ADK session state (set by whoever clones the
repo before the agent runs, see app.core.repo.clone_repo) and operates relative to it.
"""

from pathlib import Path

from google.adk.tools import ToolContext

MAX_RESULTS = 200
MAX_FILE_BYTES = 200_000

_DEPENDENCY_FILES = {
    "requirements.txt": "pip",
    "pyproject.toml": "python",
    "package.json": "npm",
    "go.mod": "go",
    "Gemfile": "bundler",
}


def _workspace(tool_context: ToolContext) -> Path:
    path = tool_context.state.get("workspace_path")
    if not path:
        raise ValueError("no repo checked out for this run (missing 'workspace_path' in state)")
    return Path(path)


def _safe_join(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    root = root.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path '{relative}' escapes the repo workspace")
    return candidate


def search_files(pattern: str, tool_context: ToolContext) -> list[str]:
    """Find repo files whose path matches a glob pattern, e.g. '**/*.py'."""
    root = _workspace(tool_context)
    matches = [
        str(p.relative_to(root)) for p in root.rglob(pattern) if p.is_file() and ".git" not in p.parts
    ]
    return matches[:MAX_RESULTS]


def read_file(path: str, tool_context: ToolContext) -> str:
    """Read a file's contents, given a path relative to the repo root."""
    root = _workspace(tool_context)
    target = _safe_join(root, path)
    if not target.is_file():
        raise ValueError(f"'{path}' is not a file in this repo")
    return target.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", errors="replace")


def search_code(query: str, file_glob: str, tool_context: ToolContext) -> list[dict]:
    """Search file contents for a literal substring; file_glob narrows which files (e.g. '**/*.py')."""
    root = _workspace(tool_context)
    results: list[dict] = []
    for p in root.rglob(file_glob):
        if not p.is_file() or ".git" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if query in line:
                results.append({"file": str(p.relative_to(root)), "line": line_no, "text": line.strip()})
                if len(results) >= MAX_RESULTS:
                    return results
    return results


def get_project_structure(max_depth: int, tool_context: ToolContext) -> str:
    """Return an indented directory tree of the repo up to max_depth."""
    root = _workspace(tool_context)
    lines: list[str] = []

    def walk(dir_path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        entries = sorted(
            (e for e in dir_path.iterdir() if e.name != ".git"),
            key=lambda e: (e.is_file(), e.name.lower()),
        )
        for entry in entries:
            lines.append(f"{'  ' * depth}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir():
                walk(entry, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def get_dependencies(tool_context: ToolContext) -> dict:
    """Return raw contents of any known dependency manifest files found at the repo root."""
    root = _workspace(tool_context)
    found: dict[str, str] = {}
    for filename, ecosystem in _DEPENDENCY_FILES.items():
        file_path = root / filename
        if file_path.is_file():
            found[ecosystem] = file_path.read_text(encoding="utf-8", errors="ignore")
    return found


REPO_TOOLS = [search_files, read_file, search_code, get_project_structure, get_dependencies]
