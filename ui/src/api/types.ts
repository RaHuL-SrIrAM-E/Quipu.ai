// Mirrors app/api/schemas/*.py exactly. Never invent a field that isn't
// actually returned by the Control Plane API — see
// docs/architecture/control_plane_api.md.

export type WorkflowStatus =
  | "pending"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "blocked"
  | "cancelled"
  | "escalated";

export type WorkflowStage =
  | "planning"
  | "architecture"
  | "codegen"
  | "testing"
  | "deployment"
  | "monitoring"
  | "completed";

export interface WorkflowRunResult {
  workflow_id: string;
  initial_stage: WorkflowStage;
  final_stage: WorkflowStage;
  final_status: WorkflowStatus;
  stages_executed: string[];
  artifacts_created: number;
  decisions_created: number;
  retries_used: number;
  duration_ms: number;
  human_action_required: boolean;
}

export interface DemoScenarioResult {
  scenario: string;
  workflow_id: string | null;
  signal_ids: string[];
  detection_id: string | null;
  review_id: string | null;
  resolution_id: string | null;
  verification_status: string;
  already_seeded: boolean;
}

export interface WorkflowSummary {
  workflow_id: string;
  ticket_title: string;
  status: WorkflowStatus;
  current_stage: WorkflowStage;
  artifact_count: number;
  remediation_outcome: string | null;
}

export interface WorkflowDetail {
  workflow_id: string;
  ticket_title: string;
  ticket_description: string;
  status: WorkflowStatus;
  current_stage: WorkflowStage;
  artifact_ids: string[];
  execution_ids: string[];
  active_decision_id: string | null;
  active_incident_ids: string[];
  remediation_outcome: string | null;
  remediation_strategy: string | null;
  latest_verification_id: string | null;
  source_detection_id: string | null;
  review_id: string | null;
}

export interface ArtifactSummary {
  artifact_id: string;
  artifact_type: string;
  version: number;
  created_by: string;
  created_at: string;
  status: WorkflowStatus;
}

export interface ExecutionSummary {
  execution_id: string;
  agent_name: string;
  status: WorkflowStatus;
  started_at: string;
  completed_at: string | null;
  retry_count: number;
  error_code: string | null;
  error_message: string | null;
}

export interface DecisionSummary {
  decision_id: string;
  action: string;
  target_agent: string | null;
  reason: string;
  confidence: number;
  source: string;
  created_at: string;
}

export type SignalDomainGuess = "operational" | "product";

export interface SignalSummary {
  signal_id: string;
  signal_type: string;
  source: string;
  severity: string;
  status: string;
  observed_at: string;
  subject: string;
  summary: string;
  service_name: string | null;
  environment: string | null;
  revision: string | null;
}

export interface SignalDetail extends SignalSummary {
  ingested_at: string;
  deployment_artifact_id: string | null;
  evidence: Record<string, unknown>;
  source_system: string;
  source_uri: string | null;
  trace_id: string | null;
}

export interface DetectionSummary {
  detection_id: string;
  detection_type: "incident" | "feature_opportunity" | "no_action";
  domain: "operational" | "product";
  title: string;
  summary: string;
  rationale: string;
  confidence: number;
  severity: string | null;
  subject: string;
  service_name: string | null;
  environment: string | null;
  supporting_signal_ids: string[];
  knowledge_references: string[];
  observation_window_minutes: number;
  detected_at: string;
}

export interface ResolutionSummary {
  resolution_id: string;
  detection_id: string;
  workflow_id: string | null;
  diagnosis_summary: string;
  probable_root_cause: string;
  root_cause_confidence: number;
  remediation_strategy: string;
  remediation_rationale: string;
  expected_outcome: string;
  risk: string;
  severity: string | null;
  escalation_recommended: boolean;
  target_agent: string | null;
  supporting_signal_ids: string[];
  supporting_artifact_ids: string[];
  resolved_at: string;
}

export type VerificationOutcome =
  | "verified_resolved"
  | "still_degraded"
  | "insufficient_evidence"
  | "escalated";

export interface VerificationSummary {
  verification_id: string;
  resolution_id: string;
  workflow_id: string;
  deployment_artifact_id: string;
  revision: string | null;
  status: "in_progress" | "completed";
  outcome: VerificationOutcome | null;
  reason: string;
  confidence: number | null;
  baseline_detection_id: string;
  baseline_signal_ids: string[];
  baseline_summary: string;
  post_deployment_signal_ids: string[];
  supporting_signal_ids: string[];
  evidence_summary: Record<string, string>;
  verification_started_at: string;
  verification_completed_at: string | null;
}

export interface FeatureReviewSummary {
  review_id: string;
  detection_id: string;
  status: "pending" | "approved" | "rejected";
  reviewer_id: string | null;
  reviewer_type: string | null;
  review_comment: string | null;
  reviewed_at: string | null;
  ticket_id: string | null;
  ticket_title: string | null;
  workflow_id: string | null;
  created_at: string;
}

export interface ApiErrorResponse {
  error: string;
  detail: string;
  correlation_id: string | null;
}
