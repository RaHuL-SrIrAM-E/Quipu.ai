"""CLI: give a feature request, get the Planning agent's task plan back."""

import asyncio

from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agents.planning import planning_agent


async def run(feature_request: str) -> str:
    runner = InMemoryRunner(agent=planning_agent, app_name="quipu")
    session = await runner.session_service.create_session(app_name="quipu", user_id="cli")
    message = types.Content(role="user", parts=[types.Part(text=feature_request)])

    final_text = ""
    async for event in runner.run_async(user_id="cli", session_id=session.id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text

    return final_text


def main() -> None:
    feature_request = input("Feature request: ")
    print(asyncio.run(run(feature_request)))


if __name__ == "__main__":
    main()
