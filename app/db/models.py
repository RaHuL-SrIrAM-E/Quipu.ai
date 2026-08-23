import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    feature_request: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    stage_runs: Mapped[list["StageRun"]] = relationship(back_populates="pipeline_run")


class StageRun(Base):
    """One execution of one agent stage within a pipeline run — the unit observability/cost tracking hangs off."""

    __tablename__ = "stage_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    pipeline_run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.id"))
    stage_name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    pipeline_run: Mapped["PipelineRun"] = relationship(back_populates="stage_runs")
