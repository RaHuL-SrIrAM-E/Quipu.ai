"""Clones a public git repo into a per-run workspace for the repo-aware tools to read from."""

import shutil
import subprocess
from pathlib import Path

from app.config import get_settings
from app.core.observability import get_logger

logger = get_logger("quipu.repo")


class RepoCloneError(Exception):
    pass


def clone_repo(repo_url: str, run_id: str, ref: str | None = None) -> Path:
    """Shallow-clones repo_url into <workspace_root>/<run_id> and returns that path."""
    settings = get_settings()
    dest = Path(settings.workspace_root) / run_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [repo_url, str(dest)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RepoCloneError(f"failed to clone {repo_url}: {result.stderr.strip()}")

    logger.info("cloned %s (ref=%s) into %s", repo_url, ref, dest)
    return dest


def cleanup_workspace(run_id: str) -> None:
    settings = get_settings()
    dest = Path(settings.workspace_root) / run_id
    if dest.exists():
        shutil.rmtree(dest)
