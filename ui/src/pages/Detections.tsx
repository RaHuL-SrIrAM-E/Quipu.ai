import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView } from "../components/States";
import { StatusBadge, DomainBadge } from "../components/StatusBadge";
import { formatDateTime } from "../lib/format";

export function Detections() {
  const [domain, setDomain] = useState<string | undefined>();
  const detections = useApiData(() => api.listDetections({ domain, limit: 100 }), [domain], 15_000);

  return (
    <>
      <PageHeader
        title="Detection Center"
        description="Quipu detects more than production failures — customer feedback, support signals, feature-request patterns, user behavior, and adoption anomalies all feed the same Detecting Agent."
      />

      <div className="filter-bar">
        <button type="button" className={!domain ? "btn btn-ghost btn-active" : "btn btn-ghost"} onClick={() => setDomain(undefined)}>
          All
        </button>
        <button type="button" className={domain === "product" ? "btn btn-ghost btn-active" : "btn btn-ghost"} onClick={() => setDomain("product")}>
          Product Opportunities
        </button>
        <button type="button" className={domain === "operational" ? "btn btn-ghost btn-active" : "btn btn-ghost"} onClick={() => setDomain("operational")}>
          Incidents
        </button>
      </div>

      <Panel>
        <DataView loading={detections.loading} error={detections.error} data={detections.data} onRetry={detections.refresh} isEmpty={(d) => d.length === 0} emptyMessage="No detections yet.">
          {(data) => (
            <div className="detection-grid">
              {data.map((d) => (
                <article key={d.detection_id} className={`detection-card detection-card-${d.domain}`}>
                  <div className="detection-card-header">
                    <DomainBadge domain={d.domain} />
                    <StatusBadge status={d.detection_type} />
                  </div>
                  <h3>{d.title}</h3>
                  <p>{d.summary}</p>
                  <div className="detection-card-meta">
                    <span>{Math.round(d.confidence * 100)}% confidence</span>
                    {d.severity && <span>· {d.severity}</span>}
                    <span>· {formatDateTime(d.detected_at)}</span>
                  </div>
                  <div className="detection-card-meta">
                    {d.supporting_signal_ids.length} supporting signal{d.supporting_signal_ids.length === 1 ? "" : "s"}
                    {d.knowledge_references.length > 0 && ` · ${d.knowledge_references.length} knowledge reference(s)`}
                  </div>
                  {d.detection_type === "feature_opportunity" && (
                    <Link className="btn btn-ghost btn-small" to="/feature-reviews">
                      View in review queue
                    </Link>
                  )}
                  {d.detection_type === "incident" && (
                    <Link className="btn btn-ghost btn-small" to="/resolutions">
                      View diagnosis
                    </Link>
                  )}
                </article>
              ))}
            </div>
          )}
        </DataView>
      </Panel>
    </>
  );
}
