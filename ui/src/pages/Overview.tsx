import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView } from "../components/States";
import { StatusBadge, DomainBadge } from "../components/StatusBadge";
import { StageTimeline } from "../components/StageTimeline";
import { formatRelativeTime } from "../lib/format";

const POLL_MS = 12_000;

export function Overview() {
  const workflows = useApiData(() => api.listWorkflows({ limit: 20 }), [], POLL_MS);
  const signals = useApiData(() => api.listSignals({ limit: 8 }), [], POLL_MS);
  const detections = useApiData(() => api.listDetections({ limit: 8 }), [], POLL_MS);
  const reviews = useApiData(() => api.listFeatureReviews({ limit: 20 }), [], POLL_MS);
  const resolutions = useApiData(() => api.listResolutions({ limit: 8 }), [], POLL_MS);
  const verifications = useApiData(() => api.listVerifications({ limit: 8 }), [], POLL_MS);

  const pendingReviews = reviews.data?.filter((r) => r.status === "pending") ?? [];
  const featured = workflows.data?.[0] ?? null;

  return (
    <>
      <PageHeader
        title="Command Center"
        description="Quipu detects what needs to change, decides what should happen next, executes the engineering workflow, monitors production, and verifies whether the change actually solved the problem."
      />

      <Panel title="Live Workflow Timeline">
        <DataView
          loading={workflows.loading}
          error={workflows.error}
          data={workflows.data}
          onRetry={workflows.refresh}
          isEmpty={(d) => d.length === 0}
          emptyMessage="No active workflows. Signals will trigger detection, review, and engineering work as they arrive."
        >
          {() =>
            featured ? (
              <div className="featured-workflow">
                <div className="featured-workflow-meta">
                  <Link to={`/workflows/${featured.workflow_id}`} className="featured-workflow-title">
                    {featured.ticket_title}
                  </Link>
                  <StatusBadge status={featured.status} />
                </div>
                <StageTimeline currentStage={featured.current_stage} status={featured.status} remediationOutcome={featured.remediation_outcome} />
              </div>
            ) : null
          }
        </DataView>
      </Panel>

      <div className="overview-grid">
        <Panel title="Active Workflows" actions={<Link to="/workflows">View all</Link>}>
          <DataView loading={workflows.loading} error={workflows.error} data={workflows.data} isEmpty={(d) => d.length === 0} emptyMessage="No workflows yet.">
            {(data) => (
              <ul className="compact-list">
                {data.slice(0, 6).map((w) => (
                  <li key={w.workflow_id}>
                    <Link to={`/workflows/${w.workflow_id}`}>{w.ticket_title}</Link>
                    <StatusBadge status={w.status} />
                  </li>
                ))}
              </ul>
            )}
          </DataView>
        </Panel>

        <Panel title="Recent Signals" actions={<Link to="/signals">Explore</Link>}>
          <DataView loading={signals.loading} error={signals.error} data={signals.data} isEmpty={(d) => d.length === 0} emptyMessage="No production or product signals observed yet.">
            {(data) => (
              <ul className="compact-list">
                {data.map((s) => (
                  <li key={s.signal_id}>
                    <span>{s.subject}</span>
                    <StatusBadge status={s.severity} />
                  </li>
                ))}
              </ul>
            )}
          </DataView>
        </Panel>

        <Panel title="Recent Detections" actions={<Link to="/detections">Explore</Link>}>
          <DataView loading={detections.loading} error={detections.error} data={detections.data} isEmpty={(d) => d.length === 0} emptyMessage="Detecting Agent has not produced a conclusion yet.">
            {(data) => (
              <ul className="compact-list">
                {data.map((d) => (
                  <li key={d.detection_id}>
                    <span>{d.title}</span>
                    <DomainBadge domain={d.domain} />
                  </li>
                ))}
              </ul>
            )}
          </DataView>
        </Panel>

        <Panel title="Feature Review Queue" actions={<Link to="/feature-reviews">Review</Link>}>
          <DataView loading={reviews.loading} error={reviews.error} data={reviews.data} isEmpty={() => pendingReviews.length === 0} emptyMessage="No feature opportunities awaiting review.">
            {() => (
              <ul className="compact-list">
                {pendingReviews.slice(0, 6).map((r) => (
                  <li key={r.review_id}>
                    <span>Detection {r.detection_id.slice(0, 8)}</span>
                    <StatusBadge status={r.status} />
                  </li>
                ))}
              </ul>
            )}
          </DataView>
        </Panel>

        <Panel title="Incidents & Remediation" actions={<Link to="/resolutions">View all</Link>}>
          <DataView loading={resolutions.loading} error={resolutions.error} data={resolutions.data} isEmpty={(d) => d.length === 0} emptyMessage="No incidents diagnosed yet.">
            {(data) => (
              <ul className="compact-list">
                {data.map((r) => (
                  <li key={r.resolution_id}>
                    <Link to={`/resolutions/${r.resolution_id}`}>{r.probable_root_cause.slice(0, 48)}</Link>
                    <StatusBadge status={r.remediation_strategy} />
                  </li>
                ))}
              </ul>
            )}
          </DataView>
        </Panel>

        <Panel title="Verification Status" actions={<Link to="/verifications">View all</Link>}>
          <DataView loading={verifications.loading} error={verifications.error} data={verifications.data} isEmpty={(d) => d.length === 0} emptyMessage="No verification records yet — deployment success is never reported as resolution on its own.">
            {(data) => (
              <ul className="compact-list">
                {data.map((v) => (
                  <li key={v.verification_id}>
                    <span>{formatRelativeTime(v.verification_started_at)}</span>
                    <StatusBadge status={v.outcome ?? v.status} />
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
