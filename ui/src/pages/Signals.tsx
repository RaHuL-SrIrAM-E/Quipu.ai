import { useState } from "react";
import { api } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView, ErrorState, LoadingState } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { formatDateTime } from "../lib/format";

const SIGNAL_TYPES = [
  "metric_anomaly",
  "log_error",
  "application_error",
  "deployment_event",
  "availability_degradation",
  "latency_anomaly",
  "customer_feedback",
  "support_feedback",
  "feature_request_pattern",
  "user_behavior",
  "adoption_anomaly",
];
const SOURCES = ["cloud_monitoring", "cloud_logging", "cloud_run", "customer_feedback", "support_system", "product_analytics", "user_behavior", "internal_system"];
const SEVERITIES = ["info", "warning", "error", "critical"];
const STATUSES = ["observed", "ingested", "available"];

const OPERATIONAL_TYPES = new Set(["metric_anomaly", "log_error", "application_error", "deployment_event", "availability_degradation", "latency_anomaly"]);

export function Signals() {
  const [filters, setFilters] = useState<{ signal_type?: string; source?: string; severity?: string; status?: string; service_name?: string; environment?: string }>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const signals = useApiData(() => api.listSignals({ ...filters, limit: 100 }), [JSON.stringify(filters)], 15_000);
  const detail = useApiData(() => (selectedId ? api.getSignal(selectedId) : Promise.resolve(null)), [selectedId]);

  return (
    <>
      <PageHeader title="Signal Explorer" description="What Quipu has observed — both operational production telemetry and product/customer signals." />

      <Panel title="Filters">
        <form className="filter-bar" onSubmit={(e) => e.preventDefault()}>
          <select aria-label="Signal type" value={filters.signal_type ?? ""} onChange={(e) => setFilters((f) => ({ ...f, signal_type: e.target.value || undefined }))}>
            <option value="">Any type</option>
            {SIGNAL_TYPES.map((t) => (
              <option key={t} value={t}>
                {t.replace(/_/g, " ")}
              </option>
            ))}
          </select>
          <select aria-label="Source" value={filters.source ?? ""} onChange={(e) => setFilters((f) => ({ ...f, source: e.target.value || undefined }))}>
            <option value="">Any source</option>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </select>
          <select aria-label="Severity" value={filters.severity ?? ""} onChange={(e) => setFilters((f) => ({ ...f, severity: e.target.value || undefined }))}>
            <option value="">Any severity</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select aria-label="Status" value={filters.status ?? ""} onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value || undefined }))}>
            <option value="">Any status</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <input aria-label="Service name" placeholder="service name" value={filters.service_name ?? ""} onChange={(e) => setFilters((f) => ({ ...f, service_name: e.target.value || undefined }))} />
          <input aria-label="Environment" placeholder="environment" value={filters.environment ?? ""} onChange={(e) => setFilters((f) => ({ ...f, environment: e.target.value || undefined }))} />
        </form>
      </Panel>

      <div className="split-layout">
        <Panel title="Signals">
          <DataView loading={signals.loading} error={signals.error} data={signals.data} onRetry={signals.refresh} isEmpty={(d) => d.length === 0} emptyMessage="No signals matched these filters.">
            {(data) => (
              <ul className="selectable-list">
                {data.map((s) => (
                  <li key={s.signal_id}>
                    <button type="button" className={selectedId === s.signal_id ? "selectable-row selected" : "selectable-row"} onClick={() => setSelectedId(s.signal_id)}>
                      <span className={`domain-dot ${OPERATIONAL_TYPES.has(s.signal_type) ? "domain-dot-operational" : "domain-dot-product"}`} aria-hidden="true" />
                      <span className="selectable-row-main">
                        <strong>{s.subject}</strong>
                        <span className="selectable-row-meta">{s.signal_type.replace(/_/g, " ")} · {s.source.replace(/_/g, " ")}</span>
                      </span>
                      <StatusBadge status={s.severity} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </DataView>
        </Panel>

        <Panel title="Signal Detail">
          {!selectedId && <p className="muted">Select a signal to view its safe provenance and evidence summary.</p>}
          {selectedId && detail.loading && <LoadingState />}
          {selectedId && detail.error && <ErrorState error={detail.error} onRetry={detail.refresh} />}
          {selectedId && detail.data && (
            <div className="signal-detail">
              <div className="signal-detail-header">
                <span className={`domain-badge domain-${OPERATIONAL_TYPES.has(detail.data.signal_type) ? "operational" : "product"}`}>
                  {OPERATIONAL_TYPES.has(detail.data.signal_type) ? "Operational" : "Product"}
                </span>
                <StatusBadge status={detail.data.severity} />
              </div>
              <h3>{detail.data.subject}</h3>
              <p>{detail.data.summary}</p>
              <dl className="kv-grid">
                <dt>Type</dt>
                <dd>{detail.data.signal_type}</dd>
                <dt>Source</dt>
                <dd>{detail.data.source_system}</dd>
                <dt>Observed</dt>
                <dd>{formatDateTime(detail.data.observed_at)}</dd>
                <dt>Ingested</dt>
                <dd>{formatDateTime(detail.data.ingested_at)}</dd>
                {detail.data.service_name && (
                  <>
                    <dt>Service</dt>
                    <dd>{detail.data.service_name}</dd>
                  </>
                )}
                {detail.data.environment && (
                  <>
                    <dt>Environment</dt>
                    <dd>{detail.data.environment}</dd>
                  </>
                )}
                {detail.data.revision && (
                  <>
                    <dt>Revision</dt>
                    <dd>{detail.data.revision}</dd>
                  </>
                )}
              </dl>
              <h4>Evidence (sanitized)</h4>
              <pre className="evidence-block">{JSON.stringify(detail.data.evidence, null, 2)}</pre>
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}
