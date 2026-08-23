"""In-process cost/latency accumulation for a single pipeline run.

StageRun rows in the DB are the durable record; this is a lightweight
running aggregate the orchestrator can use for budget checks mid-run.
"""

from dataclasses import dataclass, field


@dataclass
class RunMetrics:
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    stage_costs: dict[str, float] = field(default_factory=dict)

    def record(self, stage: str, *, cost_usd: float, latency_ms: float) -> None:
        self.total_cost_usd += cost_usd
        self.total_latency_ms += latency_ms
        self.stage_costs[stage] = self.stage_costs.get(stage, 0.0) + cost_usd

    def exceeds_budget(self, max_cost_usd: float) -> bool:
        return self.total_cost_usd > max_cost_usd
