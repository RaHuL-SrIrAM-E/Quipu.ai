import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";

export function Workflows() {
  const workflows = useApiData(() => api.listWorkflows({ limit: 100 }), [], 15_000);

  return (
    <>
      <PageHeader title="Workflows" description="Every engineering workflow Quipu has started — from an approved feature or an authorized remediation." />
      <Panel>
        <DataView loading={workflows.loading} error={workflows.error} data={workflows.data} onRetry={workflows.refresh} isEmpty={(d) => d.length === 0} emptyMessage="No workflows yet.">
          {(data) => (
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Ticket</th>
                  <th scope="col">Stage</th>
                  <th scope="col">Status</th>
                  <th scope="col">Artifacts</th>
                  <th scope="col">Remediation</th>
                </tr>
              </thead>
              <tbody>
                {data.map((w) => (
                  <tr key={w.workflow_id}>
                    <td>
                      <Link to={`/workflows/${w.workflow_id}`}>{w.ticket_title}</Link>
                    </td>
                    <td>{w.current_stage}</td>
                    <td>
                      <StatusBadge status={w.status} />
                    </td>
                    <td>{w.artifact_count}</td>
                    <td>{w.remediation_outcome ? <StatusBadge status={w.remediation_outcome} /> : "—"}</td>
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
