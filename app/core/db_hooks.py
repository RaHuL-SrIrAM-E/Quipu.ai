"""Generic ADK before/after agent callbacks that persist each stage's execution as a StageRun row.

Attach stage_started(name) / stage_completed(name, output_key) to any agent's
before_agent_callback / after_agent_callback list to get this for free.
"""

from datetime import datetime

from google.adk.agents.callback_context import CallbackContext

from app.db.base import SessionLocal
from app.db.models import StageRun


def stage_started(stage_name: str):
    def _callback(callback_context: CallbackContext) -> None:
        run_id = callback_context.state.get("run_id")
        if not run_id:
            return
        db = SessionLocal()
        try:
            stage_run = StageRun(
                pipeline_run_id=run_id,
                stage_name=stage_name,
                status="running",
                started_at=datetime.utcnow(),
            )
            db.add(stage_run)
            db.commit()
            callback_context.state[f"_stage_run_id_{stage_name}"] = stage_run.id
        finally:
            db.close()

    return _callback


def stage_completed(stage_name: str, output_state_key: str):
    def _callback(callback_context: CallbackContext) -> None:
        stage_run_id = callback_context.state.get(f"_stage_run_id_{stage_name}")
        if not stage_run_id:
            return
        db = SessionLocal()
        try:
            stage_run = db.get(StageRun, stage_run_id)
            if stage_run is None:
                return
            stage_run.status = "completed"
            stage_run.completed_at = datetime.utcnow()
            stage_run.output = callback_context.state.get(output_state_key)
            db.commit()
        finally:
            db.close()

    return _callback
