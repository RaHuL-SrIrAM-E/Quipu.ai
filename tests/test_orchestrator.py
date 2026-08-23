from app.orchestrator.graph import STAGE_ORDER, build_graph


def test_pipeline_runs_all_stages_in_order():
    graph = build_graph()

    result = graph.invoke(
        {
            "run_id": "test-run",
            "feature_request": "add dark mode",
            "stage_outputs": {},
            "current_stage": "",
            "errors": [],
        }
    )

    assert list(result["stage_outputs"].keys()) == STAGE_ORDER
    assert result["current_stage"] == STAGE_ORDER[-1]
    assert result["errors"] == []
