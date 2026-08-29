import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { StageTimeline } from "../components/StageTimeline";
import { CommandButton } from "../components/CommandButton";
import { formatDateTime } from "../lib/format";

const ARTIFACT_LINEAGE_ORDER = ["plan", "architecture", "code_change", "test_result", "deployment"];

export function WorkflowDetail() {
  const { workflowId } = useParams<{ workflowId: string }>();
  const id = workflowId!;

  const workflow = useApiData(() => api.getWorkflow(id), [id], 8_000);
  const artifacts = useApiData(() => api.listWorkflowArtifacts(id), [id], 8_000);
  const executions = useApiData(() => api.listWorkflowExecutions(id), [id], 8_000);
  const decisions = useApiData(() => api.listWorkflowDecisions(id), [id], 8_000);

  const isActive = workflow.data ? !["completed", "failed", "cancelled", "escalated"].includes(workflow.data.status) : false;

  return (
    <>
      <DataView loading={workflow.loading} error={workflow.error} data={workflow.data} onRetry={workflow.refresh} isEmpty={() => false} emptyMessage="">
        {(w) => (
          <>
            <PageHeader
              title={w.ticket_title}
              description={w.ticket_description}
              actions={
                isActive ? (
                  <CommandButton
                    label="Run Next Step"
                    confirmLabel="Run step"
                    description="This will advance the workflow exactly one stage forward through OrchestrationService.execute_next_step — the same step-wise execution every agent invocation already uses."
                    onRun={() => api.stepWorkflow(id)}
                    onSuccess={() => {
                      workflow.refresh();
                      artifacts.refresh();
                      executions.refresh();
                      decisions.refresh();
                    }}
                  />
                ) : null
              }
            />

            <Panel title="Progress">
              <StatusBadge status={w.status} />
              <div style={{ marginTop: "1rem" }}>
                <StageTimeline currentStage={w.current_stage} status={w.status} remediationOutcome={w.remediation_outcome} />
              </div>
            </Panel>

            {(w.source_detection_id || w.review_id || w.remediation_strategy) && (
              <Panel title="Provenance">
                <dl className="kv-grid">
                  {w.source_detection_id && (
                    <>
                      <dt>Source detection</dt>
                      <dd>{w.source_detection_id}</dd>
                    </>
                  )}
                  {w.review_id && (
                    <>
                      <dt>Feature review</dt>
                      <dd>{w.review_id}</dd>
                    </>
                  )}
                  {w.remediation_strategy && (
                    <>
                      <dt>Remediation strategy</dt>
                      <dd>{w.remediation_strategy}</dd>
                    </>
                  )}
                  {w.latest_verification_id && (
                    <>
                      <dt>Latest verification</dt>
                      <dd>{w.latest_verification_id}</dd>
                    </>
                  )}
                </dl>
              </Panel>
            )}
          </>
        )}
      </DataView>

      <Panel title="Artifact Lineage">
        <DataView loading={artifacts.loading} error={artifacts.error} data={artifacts.data} isEmpty={(d) => d.length === 0} emptyMessage="No artifacts produced yet.">
          {(data) => {
            const byType = new Map(data.map((a) => [a.artifact_type, a]));
            return (
              <ol className="lineage-row">
                {ARTIFACT_LINEAGE_ORDER.map((type) => {
                  const artifact = byType.get(type);
                  return (
                    <li key={type} className={artifact ? "lineage-item lineage-done" : "lineage-item lineage-pending"}>
                      <span className="lineage-type">{type.replace(/_/g, " ")}</span>
                      {artifact ? (
                        <>
                          <StatusBadge status={artifact.status} />
                          <span className="lineage-meta">v{artifact.version} · {formatDateTime(artifact.created_at)}</span>
                        </>
                      ) : (
                        <span className="lineage-meta">not yet produced</span>
                      )}
                    </li>
                  );
                })}
              </ol>
            );
          }}
        </DataView>
      </Panel>

      <div className="two-col">
        <Panel title="Agent Executions">
          <DataView loading={executions.loading} error={executions.error} data={executions.data} isEmpty={(d) => d.length === 0} emptyMessage="No agent has run yet.">
            {(data) => (
              <ul className="record-list">
                {data.map((e) => (
                  <li key={e.execution_id}>
                    <div className="record-list-row">
                      <strong>{e.agent_name}</strong>
                      <StatusBadge status={e.status} />
                    </div>
                    <div className="record-list-meta">
                      {formatDateTime(e.started_at)} {e.completed_at ? `→ ${formatDateTime(e.completed_at)}` : ""}
                      {e.retry_count > 0 && ` · retry ${e.retry_count}`}
                    </div>
                    {e.error_message && <div className="record-list-error">{e.error_code}: {e.error_message}</div>}
                  </li>
                ))}
              </ul>
            )}
          </DataView>
        </Panel>

        <Panel title="Orchestrator Decisions">
          <DataView loading={decisions.loading} error={decisions.error} data={decisions.data} isEmpty={(d) => d.length === 0} emptyMessage="No orchestration decision recorded yet.">
            {(data) => (
              <ul className="record-list">
                {data.map((d) => (
                  <li key={d.decision_id}>
                    <div className="record-list-row">
                      <strong>{d.action}</strong>
                      <span>{Math.round(d.confidence * 100)}% confidence</span>
                    </div>
                    <div className="record-list-meta">
                      {d.source} · {formatDateTime(d.created_at)}
                      {d.target_agent && ` · → ${d.target_agent}`}
                    </div>
                    <div>{d.reason}</div>
                  </li>
                ))}
              </ul>
            )}
          </DataView>
        </Panel>
      </div>
    </>
  );
}
