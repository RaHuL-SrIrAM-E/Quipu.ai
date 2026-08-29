"""CLI entry point for the end-to-end Quipu demo.

    python -m app.demo.run --scenario feature
    python -m app.demo.run --scenario incident
    python -m app.demo.run --scenario both

Runs entirely in-memory, with no live Gemini/Jira/Cloud Monitoring/Cloud
Logging/Cloud Run/Firestore credentials required — see
docs/architecture/end_to_end_demo.md for what's real vs. faked. Exits
non-zero if any scenario's verification_status is "failed", so this is
also usable as a CI smoke check.
"""

import argparse
import asyncio
import json
import sys

from app.demo.harness import DemoHarness
from app.demo.summary import DemoSummary


async def _run(scenario: str) -> list[DemoSummary]:
    harness = DemoHarness()
    summaries: list[DemoSummary] = []
    if scenario in ("feature", "both"):
        summaries.append(await harness.run_feature_flow())
    if scenario in ("incident", "both"):
        summaries.append(await harness.run_incident_flow())
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Quipu end-to-end demo scenarios.")
    parser.add_argument("--scenario", choices=["feature", "incident", "both"], default="both")
    args = parser.parse_args()

    summaries = asyncio.run(_run(args.scenario))

    for summary in summaries:
        print(f"\n{'=' * 70}\nSCENARIO: {summary.scenario}\n{'=' * 70}")
        for step in summary.steps:
            mark = "PASS" if step.passed else "FAIL"
            print(f"  [{mark}] {step.name}: {step.detail}")
        print(json.dumps(summary.model_dump(mode="json"), indent=2))

    any_failed = any(s.verification_status != "passed" for s in summaries)
    if any_failed:
        print("\nOne or more scenarios FAILED verification.", file=sys.stderr)
        sys.exit(1)
    print("\nAll scenarios verified successfully.")


if __name__ == "__main__":
    main()
