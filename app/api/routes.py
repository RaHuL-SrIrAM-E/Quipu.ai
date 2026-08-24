from fastapi import APIRouter
from pydantic import BaseModel

from app.orchestrator.run import run_pipeline

router = APIRouter()


class RunRequest(BaseModel):
    repo_url: str | None = None
    ref: str | None = None


class RunResponse(BaseModel):
    state: dict


@router.post("/runs", response_model=RunResponse)
async def create_run(request: RunRequest) -> RunResponse:
    state = await run_pipeline(repo_url=request.repo_url, ref=request.ref)
    return RunResponse(state=state)


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}
