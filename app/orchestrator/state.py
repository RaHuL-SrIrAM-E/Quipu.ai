"""Shared state that flows through every node in the LangGraph pipeline."""

from typing import Annotated, Any, TypedDict


def _merge_dict(left: dict, right: dict) -> dict:
    return {**left, **right}


class PipelineState(TypedDict):
    run_id: str
    feature_request: str

    # Each stage writes its output under its own key so later stages can read
    # earlier ones without clobbering them.
    stage_outputs: Annotated[dict[str, Any], _merge_dict]

    current_stage: str
    errors: Annotated[list[str], lambda left, right: left + right]
