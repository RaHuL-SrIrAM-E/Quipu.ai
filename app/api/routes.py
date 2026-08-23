import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.metrics import RunMetrics
from app.orchestrator.graph import build_graph

router = APIRouter()


class RunRequest(BaseModel):
    feature_request: str


class RunResponse(BaseModel):
    run_id: str
    current_stage: str
    stage_outputs: dict
    errors: list[str]
    total_cost_usd: float


@router.post("/runs", response_model=RunResponse)
def create_run(request: RunRequest) -> RunResponse:
    run_id = str(uuid.uuid4())
    metrics = RunMetrics()
    graph = build_graph(metrics=metrics)

    result = graph.invoke(
        {
            "run_id": run_id,
            "feature_request": request.feature_request,
            "stage_outputs": {},
            "current_stage": "",
            "errors": [],
        }
    )

    return RunResponse(
        run_id=run_id,
        current_stage=result["current_stage"],
        stage_outputs=result["stage_outputs"],
        errors=result["errors"],
        total_cost_usd=metrics.total_cost_usd,
    )


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}
