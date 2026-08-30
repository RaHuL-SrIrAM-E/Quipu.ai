"""Clones a git repo (public, or private via a short-lived access token) into
a per-run workspace for the repo-aware tools to read from.
"""

import base64
import os
import shutil
import subprocess
from pathlib import Path

from app.config import get_settings
from app.core.observability import get_logger

logger = get_logger("quipu.repo")


class RepoCloneError(Exception):
    pass


def _auth_env(token: str | None) -> dict[str, str]:
    """Builds the subprocess-only environment that authenticates the clone,
    without ever touching disk. Deliberately NOT `-c http.extraHeader=...`
    on the command line (visible to any local `ps`/process listing) and
    NOT a token embedded in the clone URL (which git would otherwise
    persist into the checked-out repo's own .git/config, readable by the
    repo-aware tools' file access) — GIT_CONFIG_* env vars apply only to
    this one subprocess invocation and are never written anywhere, so the
    workspace itself, its .git/config, and anything search_files()/
    read_file() can see never contain the credential.
    """
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def clone_repo(repo_url: str, run_id: str, ref: str | None = None) -> Path:
    """Shallow-clones repo_url into <workspace_root>/<run_id> and returns
    that path. Authenticates via Settings.git_access_token when set (a
    private repo); a public repo needs none. See _auth_env for why the
    credential is passed as subprocess-scoped env, never as part of the
    command line or the clone URL."""
    settings = get_settings()
    dest = Path(settings.workspace_root) / run_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [repo_url, str(dest)]

    env = {**os.environ, **_auth_env(settings.git_access_token)}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if result.returncode != 0:
        raise RepoCloneError(f"failed to clone {repo_url}: {result.stderr.strip()}")

    logger.info("cloned %s (ref=%s) into %s", repo_url, ref, dest)
    return dest


def cleanup_workspace(run_id: str) -> None:
    settings = get_settings()
    dest = Path(settings.workspace_root) / run_id
    if dest.exists():
        shutil.rmtree(dest)
