"""Controlled test execution tool — the ONLY way any Quipu agent runs tests.

No shell, no arbitrary command strings. The model requests a *structured*
mode/test_paths/markers; application code builds the actual subprocess argv
list — there is no channel through this tool's signature for `rm`, `curl`,
`ssh`, pipelines, or any command construction the model could smuggle
through, because the tool never accepts a command string at all.

Every result — pass, fail, or error — is a fact reported back to the agent
AND (via tool_context.state["_test_executions"]) recorded for the agent to
treat as ground truth. See app/agents/testing.py: TestingAgent overrides
whatever the model claims about overall_status with what actually happened
here.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

from google.adk.tools import ToolContext

from app.agent_runtime.capabilities import AgentCapability
from app.config import get_settings
from app.tools.repo_tools import _safe_join, _workspace

MAX_TEST_PATHS = 20
_SAFE_MARKER_RE = re.compile(r"^[a-zA-Z0-9_ ]+$")
_SUMMARY_FIELD_RE = {
    "tests_passed": re.compile(r"(\d+) passed"),
    "tests_failed": re.compile(r"(\d+) failed"),
    "tests_skipped": re.compile(r"(\d+) skipped"),
    "_errors": re.compile(r"(\d+) error"),
}


def _detect_framework(root: Path) -> str | None:
    for filename in ("pyproject.toml", "requirements.txt"):
        candidate = root / filename
        if candidate.is_file() and "pytest" in candidate.read_text(encoding="utf-8", errors="ignore"):
            return "pytest"
    if (root / "pytest.ini").is_file():
        return "pytest"
    return None


def _parse_pytest_summary(stdout: str) -> dict:
    last_line = ""
    for line in reversed(stdout.splitlines()):
        if line.strip():
            last_line = line
            break

    counts = {}
    for key, pattern in _SUMMARY_FIELD_RE.items():
        match = pattern.search(last_line)
        counts[key] = int(match.group(1)) if match else 0

    errors = counts.pop("_errors")
    counts["tests_collected"] = counts["tests_passed"] + counts["tests_failed"] + counts["tests_skipped"] + errors
    return counts


def _result(
    *,
    success: bool,
    mode: str,
    status: str,
    command: str | None = None,
    exit_code: int | None = None,
    duration_seconds: float = 0.0,
    stdout: str = "",
    stderr: str = "",
    error: str | None = None,
    tests_collected: int = 0,
    tests_passed: int = 0,
    tests_failed: int = 0,
    tests_skipped: int = 0,
) -> dict:
    return {
        "success": success,
        "mode": mode,
        "status": status,  # "passed" | "failed" | "error"
        "command": command,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "stdout": stdout[-10_000:],
        "stderr": stderr[-10_000:],
        "error": error,
        "tests_collected": tests_collected,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "tests_skipped": tests_skipped,
    }


def run_tests(mode: str, test_paths: list[str], markers: list[str], tool_context: ToolContext) -> dict:
    """Run tests in a controlled way. mode is 'targeted' (runs exactly
    test_paths — real paths inside the repo, e.g. 'tests/test_theme.py') or
    'regression' (runs the repo's whole configured suite; test_paths is
    ignored). markers are optional pytest -m marker expressions (plain
    words, no shell syntax). This never accepts or constructs a shell
    command — only pytest, with an explicit safe argument list, is ever run.
    """
    granted: set[AgentCapability] = tool_context.state.get("_capabilities", set())
    if AgentCapability.RUN_TESTS not in granted:
        return _result(success=False, mode=mode, status="error", error="RUN_TESTS capability not granted")

    if mode not in ("targeted", "regression"):
        return _result(success=False, mode=mode, status="error", error=f"invalid mode '{mode}'")

    root = _workspace(tool_context).resolve()
    if _detect_framework(root) != "pytest":
        return _result(
            success=False, mode=mode, status="error", error="no supported test framework detected (only pytest is currently supported)"
        )

    argv = [sys.executable, "-m", "pytest", "-q"]

    if mode == "targeted":
        if not test_paths:
            return _result(success=False, mode=mode, status="error", error="targeted mode requires at least one test_path")
        if len(test_paths) > MAX_TEST_PATHS:
            return _result(success=False, mode=mode, status="error", error=f"too many test_paths (max {MAX_TEST_PATHS})")
        for path in test_paths:
            if Path(path).is_absolute():
                return _result(success=False, mode=mode, status="error", error=f"absolute test path not allowed: '{path}'")
            try:
                target = _safe_join(root, path.removeprefix("./"))
            except ValueError as exc:
                return _result(success=False, mode=mode, status="error", error=str(exc))
            argv.append(str(target.relative_to(root)))

    for marker in markers or []:
        if not _SAFE_MARKER_RE.match(marker):
            return _result(success=False, mode=mode, status="error", error=f"invalid marker expression: '{marker}'")
        argv.extend(["-m", marker])

    timeout_seconds = get_settings().test_execution_timeout_seconds
    start = time.monotonic()
    try:
        completed = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        result = _result(
            success=False,
            mode=mode,
            status="error",
            command=" ".join(argv),
            duration_seconds=time.monotonic() - start,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=f"test execution timed out after {timeout_seconds}s",
        )
        tool_context.state.setdefault("_test_executions", []).append(result)
        return result

    duration = time.monotonic() - start
    stats = _parse_pytest_summary(completed.stdout)

    # pytest exit codes: 0 all passed, 1 tests failed, 5 no tests collected,
    # 2/3/4 interrupted/internal error/usage error — all of the latter are
    # infrastructure problems, not "the code has a bug."
    if completed.returncode == 0:
        status = "passed"
    elif completed.returncode == 1:
        status = "failed"
    else:
        status = "error"

    result = _result(
        success=True,
        mode=mode,
        status=status,
        command=" ".join(argv),
        exit_code=completed.returncode,
        duration_seconds=duration,
        stdout=completed.stdout,
        stderr=completed.stderr,
        **stats,
    )
    tool_context.state.setdefault("_test_executions", []).append(result)
    return result


TESTING_TOOLS = [run_tests]
