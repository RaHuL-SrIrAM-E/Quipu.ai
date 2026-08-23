"""Drives one pipeline run: creates the PipelineRun row, executes the ADK
SequentialAgent, and returns the accumulated session state.
"""

import uuid

from google.adk.runners import InMemoryRunner
from google.genai import types

from app.core.observability import get_logger
from app.core.repo import clone_repo
from app.db.base import SessionLocal
from app.db.models import PipelineRun
from app.orchestrator.pipeline import quipu_pipeline

logger = get_logger("quipu.orchestrator")


async def run_pipeline(feature_request: str, repo_url: str | None = None, ref: str | None = None) -> dict:
    run_id = str(uuid.uuid4())

    db = SessionLocal()
    try:
        db.add(PipelineRun(id=run_id, feature_request=feature_request, status="running"))
        db.commit()
    finally:
        db.close()

    state = {"run_id": run_id}
    if repo_url:
        state["workspace_path"] = str(clone_repo(repo_url, run_id, ref=ref))

    runner = InMemoryRunner(agent=quipu_pipeline, app_name="quipu")
    session = await runner.session_service.create_session(app_name="quipu", user_id="pipeline", state=state)
    message = types.Content(role="user", parts=[types.Part(text=feature_request)])

    status = "completed"
    try:
        async for _ in runner.run_async(user_id="pipeline", session_id=session.id, new_message=message):
            pass
    except Exception:
        status = "failed"
        logger.exception("pipeline run %s failed", run_id)
        raise
    finally:
        final_session = await runner.session_service.get_session(
            app_name="quipu", user_id="pipeline", session_id=session.id
        )
        db = SessionLocal()
        try:
            pipeline_run = db.get(PipelineRun, run_id)
            pipeline_run.status = status
            db.commit()
        finally:
            db.close()

    return final_session.state
