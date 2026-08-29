import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { formatDateTime } from "../lib/format";

export function Resolutions() {
  const resolutions = useApiData(() => api.listResolutions({ limit: 100 }), [], 10_000);

  return (
    <>
      <PageHeader title="Incidents" description="Every production incident Incident Resolution has diagnosed, with its recommended remediation." />
      <Panel>
        <DataView loading={resolutions.loading} error={resolutions.error} data={resolutions.data} onRetry={resolutions.refresh} isEmpty={(d) => d.length === 0} emptyMessage="No incidents diagnosed yet.">
          {(data) => (
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Root cause</th>
                  <th scope="col">Strategy</th>
                  <th scope="col">Risk</th>
                  <th scope="col">Confidence</th>
                  <th scope="col">Resolved</th>
                </tr>
              </thead>
              <tbody>
                {data.map((r) => (
                  <tr key={r.resolution_id}>
                    <td>
                      <Link to={`/resolutions/${r.resolution_id}`}>{r.probable_root_cause}</Link>
                    </td>
                    <td>
                      <StatusBadge status={r.remediation_strategy} />
                    </td>
                    <td>{r.risk}</td>
                    <td>{Math.round(r.root_cause_confidence * 100)}%</td>
                    <td>{formatDateTime(r.resolved_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Panel>
    </>
  );
}
