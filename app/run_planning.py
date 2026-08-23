"""CLI: give a repo + feature request, get the Planning agent's task plan back.

Retries automatically if the model's plan fails PlanOutput validation, feeding
the validation errors back to the model so it can self-correct.
"""

import asyncio
import uuid

from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import ValidationError

from app.agents.planning import PlanOutput, planning_agent
from app.core.observability import get_logger
from app.core.repo import clone_repo

logger = get_logger("quipu.run_planning")

MAX_ATTEMPTS = 3


async def run(
    feature_request: str,
    repo_url: str | None,
    ref: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> str:
    run_id = str(uuid.uuid4())
    state = {}
    if repo_url:
        workspace_path = clone_repo(repo_url, run_id, ref=ref)
        state["workspace_path"] = str(workspace_path)

    runner = InMemoryRunner(agent=planning_agent, app_name="quipu")
    session = await runner.session_service.create_session(app_name="quipu", user_id="cli", state=state)

    message = types.Content(role="user", parts=[types.Part(text=feature_request)])
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        final_text = ""
        try:
            async for event in runner.run_async(user_id="cli", session_id=session.id, new_message=message):
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = event.content.parts[0].text

            if not final_text.strip():
                raise ValueError("model returned an empty response")

            PlanOutput.model_validate_json(final_text)
        except (ValidationError, ValueError) as exc:
            last_error = exc
            logger.warning("planning attempt %s/%s failed validation: %s", attempt, max_attempts, exc)
            message = types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=(
                            "Your previous plan failed validation with these errors:\n"
                            f"{exc}\n\nFix these specific issues and return a corrected plan."
                        )
                    )
                ],
            )
            continue

        return final_text

    raise RuntimeError(f"planning agent failed validation after {max_attempts} attempts") from last_error


def main() -> None:
    repo_url = input("GitHub repo URL (blank to skip): ").strip() or None
    feature_request = input("Feature request: ")
    print(asyncio.run(run(feature_request, repo_url)))


if __name__ == "__main__":
    main()
