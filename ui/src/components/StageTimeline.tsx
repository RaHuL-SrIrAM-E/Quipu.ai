import type { WorkflowStage, WorkflowStatus } from "../api/types";

const SDLC_STAGES: { key: WorkflowStage; label: string }[] = [
  { key: "planning", label: "Planning" },
  { key: "architecture", label: "Architecture" },
  { key: "codegen", label: "Codegen" },
  { key: "testing", label: "Testing" },
  { key: "deployment", label: "Deployment" },
  { key: "completed", label: "Completed" },
];

const STAGE_INDEX: Record<WorkflowStage, number> = Object.fromEntries(SDLC_STAGES.map((s, i) => [s.key, i])) as Record<WorkflowStage, number>;

function stepState(stageIndex: number, currentIndex: number, status: WorkflowStatus): "done" | "active" | "failed" | "upcoming" {
  if (stageIndex < currentIndex) return "done";
  if (stageIndex > currentIndex) return "upcoming";
  if (status === "failed" || status === "escalated") return "failed";
  if (status === "completed") return "done";
  return "active";
}

/**
 * The SDLC pipeline visualization — Signal/Detection/Review live outside
 * WorkflowState (see docs/architecture/control_plane_api.md), so this
 * component renders only the stages the API's WorkflowDetail/Summary
 * actually carries (current_stage/status), never fabricated ones.
 */
export function StageTimeline({
  currentStage,
  status,
  remediationOutcome,
}: {
  currentStage: WorkflowStage;
  status: WorkflowStatus;
  remediationOutcome?: string | null;
}) {
  const currentIndex = STAGE_INDEX[currentStage] ?? 0;

  return (
    <ol className="stage-timeline" aria-label="Workflow stage progress">
      {SDLC_STAGES.map((stage, i) => {
        const state = stepState(i, currentIndex, status);
        return (
          <li key={stage.key} className={`stage-step stage-${state}`}>
            <span className="stage-marker" aria-hidden="true" />
            <span className="stage-label">{stage.label}</span>
            <span className="stage-state-text">{state === "done" ? "done" : state === "active" ? "in progress" : state === "failed" ? "failed" : "upcoming"}</span>
          </li>
        );
      })}
      {remediationOutcome && (
        <li className={`stage-step ${remediationOutcome === "verified_resolved" ? "stage-done" : remediationOutcome === "deployed_pending_verification" ? "stage-active" : "stage-warn"}`}>
          <span className="stage-marker" aria-hidden="true" />
          <span className="stage-label">Verification</span>
          <span className="stage-state-text">{remediationOutcome.replace(/_/g, " ")}</span>
        </li>
      )}
    </ol>
  );
}
