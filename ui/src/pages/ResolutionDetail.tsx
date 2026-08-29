import { useParams, Link } from "react-router-dom";
import { api } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { StageTimeline } from "../components/StageTimeline";
import { CommandButton } from "../components/CommandButton";
import { formatDateTime } from "../lib/format";

const OUTCOME_COPY: Record<string, string> = {
  verified_resolved: "Fresh production evidence confirms the original condition cleared.",
  still_degraded: "Fresh production evidence shows the original condition is still present.",
  insufficient_evidence: "Not enough post-deployment evidence exists to conclude safely — never treated as success.",
  escalated: "Verification itself hit a condition that requires human attention.",
};

export function ResolutionDetail() {
  const { resolutionId } = useParams<{ resolutionId: string }>();
  const id = resolutionId!;

  const resolution = useApiData(() => api.getResolution(id), [id], 10_000);
  const workflowId = resolution.data?.workflow_id ?? null;
  const workflow = useApiData(() => (workflowId ? api.getWorkflow(workflowId) : Promise.resolve(null)), [workflowId], 8_000);
  const verificationId = workflow.data?.latest_verification_id ?? null;
  const verification = useApiData(() => (verificationId ? api.getVerification(verificationId) : Promise.resolve(null)), [verificationId], 10_000);

  return (
    <DataView loading={resolution.loading} error={resolution.error} data={resolution.data} onRetry={resolution.refresh} isEmpty={() => false} emptyMessage="">
      {(r) => (
        <>
          <PageHeader title="Incident Diagnosis" description={r.diagnosis_summary} />

          <div className="two-col">
            <Panel title="Diagnosis">
              <dl className="kv-grid">
                <dt>Probable root cause</dt>
                <dd>{r.probable_root_cause}</dd>
                <dt>Root cause confidence</dt>
                <dd>{Math.round(r.root_cause_confidence * 100)}%</dd>
                <dt>Risk</dt>
                <dd>{r.risk}</dd>
                {r.severity && (
                  <>
                    <dt>Severity</dt>
                    <dd>{r.severity}</dd>
                  </>
                )}
                <dt>Remediation strategy</dt>
                <dd>
                  <StatusBadge status={r.remediation_strategy} />
                </dd>
                <dt>Expected outcome</dt>
                <dd>{r.expected_outcome}</dd>
              </dl>
            </Panel>

            <Panel title="Evidence">
              <p className="muted">Rationale: {r.remediation_rationale}</p>
              <p>{r.supporting_signal_ids.length} supporting signal(s), {r.supporting_artifact_ids.length} supporting artifact(s).</p>
              <p className="muted">Detection: <Link to="/detections">{r.detection_id}</Link></p>
              {r.escalation_recommended && <p className="callout callout-warn">Escalation was recommended for this resolution.</p>}
            </Panel>
          </div>

          <Panel
            title="Remediation Timeline"
            actions={
              <CommandButton
                label="Authorize Remediation"
                confirmLabel="Authorize"
                description="This will authorize the existing remediation workflow (OrchestrationService.start_remediation_from_resolution). Quipu re-derives the target agent and strategy deterministically from this resolution — nothing here is client-selectable."
                onRun={() => api.remediateResolution(id)}
                onSuccess={() => {
                  workflow.refresh();
                }}
              />
            }
          >
            <div className="remediation-flow">
              <span>Detection</span>
              <span aria-hidden="true">→</span>
              <span>Resolution</span>
              <span aria-hidden="true">→</span>
              <span>Authorization</span>
              <span aria-hidden="true">→</span>
              <span>Codegen / Architecture</span>
              <span aria-hidden="true">→</span>
              <span>Testing</span>
              <span aria-hidden="true">→</span>
              <span>Deployment</span>
              <span aria-hidden="true">→</span>
              <span>Verification</span>
            </div>
            {workflow.data && <StageTimeline currentStage={workflow.data.current_stage} status={workflow.data.status} remediationOutcome={workflow.data.remediation_outcome} />}
            {!workflow.data && !workflow.loading && <p className="muted">Not yet authorized — click "Authorize Remediation" to start the engineering workflow.</p>}
          </Panel>

          <Panel title="Verification Result">
            <div className="verification-distinction">
              <div className={`verification-pill ${workflow.data?.status === "completed" ? "verification-pill-active" : ""}`}>Deployment Success</div>
              <span className="verification-neq" aria-hidden="true">
                ≠
              </span>
              <div className={`verification-pill ${verification.data?.outcome === "verified_resolved" ? "verification-pill-good" : ""}`}>Verified Resolved</div>
            </div>

            {!workflow.data?.latest_verification_id && <p className="muted">No verification record yet. Deployment success alone is never reported as resolution.</p>}

            {verification.data && (
              <div className={`verification-outcome verification-outcome-${verification.data.outcome ?? "pending"}`}>
                <StatusBadge status={verification.data.outcome ?? verification.data.status} />
                <p>{verification.data.outcome ? OUTCOME_COPY[verification.data.outcome] : "Verification is still in progress."}</p>
                <p className="muted">{verification.data.reason}</p>
                <p className="muted">
                  Checked {formatDateTime(verification.data.verification_completed_at ?? verification.data.verification_started_at)} · revision {verification.data.revision ?? "unknown"}
                </p>
                <Link to="/verifications">View full verification evidence →</Link>
              </div>
            )}
          </Panel>
        </>
      )}
    </DataView>
  );
}
