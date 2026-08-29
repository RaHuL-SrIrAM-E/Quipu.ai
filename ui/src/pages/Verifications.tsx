import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { formatDateTime } from "../lib/format";
import type { VerificationSummary } from "../api/types";

function VerificationCard({ v }: { v: VerificationSummary }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <article className="verification-card">
      <div className="verification-card-header">
        <StatusBadge status={v.outcome ?? v.status} />
        <span className="muted">{formatDateTime(v.verification_completed_at ?? v.verification_started_at)}</span>
      </div>
      <p>{v.reason}</p>
      <p className="muted">
        Resolution <Link to={`/resolutions/${v.resolution_id}`}>{v.resolution_id.slice(0, 8)}</Link> · revision {v.revision ?? "unknown"}
      </p>

      <button type="button" className="btn btn-ghost btn-small" onClick={() => setExpanded((e) => !e)} aria-expanded={expanded}>
        {expanded ? "Hide evidence" : "Show before / after evidence"}
      </button>

      {expanded && (
        <div className="before-after">
          <div className="before-after-col">
            <h4>Before (baseline)</h4>
            <p className="muted">{v.baseline_summary}</p>
            <p className="muted">{v.baseline_signal_ids.length} baseline signal(s)</p>
          </div>
          <div className="before-after-col">
            <h4>After (post-deployment)</h4>
            <p className="muted">{v.post_deployment_signal_ids.length} post-deployment signal(s) observed</p>
            <ul className="evidence-summary-list">
              {Object.entries(v.evidence_summary).map(([type, verdict]) => (
                <li key={type}>
                  <span>{type.replace(/_/g, " ")}</span>
                  <StatusBadge status={verdict} />
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </article>
  );
}

export function Verifications() {
  const [outcome, setOutcome] = useState<string | undefined>();
  const verifications = useApiData(() => api.listVerifications({ outcome, limit: 50 }), [outcome], 10_000);

  return (
    <>
      <PageHeader
        title="Verification"
        description={'Deployment is not resolution. Resolution is only established after fresh production evidence. Every record here compares baseline evidence against real post-deployment signals.'}
      />

      <div className="filter-bar">
        {["", "verified_resolved", "still_degraded", "insufficient_evidence", "escalated"].map((o) => (
          <button key={o || "all"} type="button" className={outcome === (o || undefined) ? "btn btn-ghost btn-active" : "btn btn-ghost"} onClick={() => setOutcome(o || undefined)}>
            {o ? o.replace(/_/g, " ") : "All"}
          </button>
        ))}
      </div>

      <Panel>
        <DataView loading={verifications.loading} error={verifications.error} data={verifications.data} onRetry={verifications.refresh} isEmpty={(d) => d.length === 0} emptyMessage="No verification records yet.">
          {(data) => (
            <div className="verification-grid">
              {data.map((v) => (
                <VerificationCard key={v.verification_id} v={v} />
              ))}
            </div>
          )}
        </DataView>
      </Panel>
    </>
  );
}
