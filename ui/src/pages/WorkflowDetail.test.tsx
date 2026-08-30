import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkflowDetail } from "./WorkflowDetail";
import type { WorkflowDetail as WorkflowDetailType } from "../api/types";

function makeWorkflow(overrides: Partial<WorkflowDetailType> = {}): WorkflowDetailType {
  return {
    workflow_id: "wf-1",
    ticket_title: "Add tag-based test filtering",
    ticket_description: "Allow tagging/grouping Karate tests.",
    status: "failed",
    current_stage: "codegen",
    artifact_ids: ["plan-1", "arch-1"],
    execution_ids: ["exec-1", "exec-2"],
    active_decision_id: null,
    active_incident_ids: [],
    remediation_outcome: null,
    remediation_strategy: null,
    latest_verification_id: null,
    source_detection_id: null,
    review_id: "review-1",
    ...overrides,
  };
}

vi.mock("../api/client", () => ({
  api: {
    getWorkflow: vi.fn(),
    listWorkflowArtifacts: vi.fn(),
    listWorkflowExecutions: vi.fn(),
    listWorkflowDecisions: vi.fn(),
    stepWorkflow: vi.fn(),
    runWorkflow: vi.fn(),
    retryWorkflow: vi.fn(),
  },
}));

import { api } from "../api/client";

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/workflows/wf-1"]}>
      <Routes>
        <Route path="/workflows/:workflowId" element={<WorkflowDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("WorkflowDetail page — retry action", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listWorkflowArtifacts).mockResolvedValue([]);
    vi.mocked(api.listWorkflowExecutions).mockResolvedValue([]);
    vi.mocked(api.listWorkflowDecisions).mockResolvedValue([]);
  });

  it("shows Retry for a FAILED workflow", async () => {
    vi.mocked(api.getWorkflow).mockResolvedValue(makeWorkflow({ status: "failed" }));
    renderPage();
    expect(await screen.findByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it.each(["pending", "running", "completed", "escalated", "cancelled"] as const)(
    "hides Retry for a %s workflow",
    async (status) => {
      vi.mocked(api.getWorkflow).mockResolvedValue(makeWorkflow({ status }));
      renderPage();
      await screen.findByText("Add tag-based test filtering");
      expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    },
  );

  it("shows Run Next Step/Run Workflow instead of Retry for active statuses", async () => {
    vi.mocked(api.getWorkflow).mockResolvedValue(makeWorkflow({ status: "pending" }));
    renderPage();
    expect(await screen.findByRole("button", { name: "Run Next Step" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run Workflow" })).toBeInTheDocument();
  });

  it("calls POST /workflows/{id}/retry (via api.retryWorkflow) and never auto-runs the workflow", async () => {
    vi.mocked(api.getWorkflow).mockResolvedValue(makeWorkflow({ status: "failed" }));
    vi.mocked(api.retryWorkflow).mockResolvedValue(makeWorkflow({ status: "pending", current_stage: "codegen" }));
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "↻ Retry" }));
    await userEvent.click(screen.getByRole("button", { name: "Retry workflow" }));

    await waitFor(() => expect(api.retryWorkflow).toHaveBeenCalledWith("wf-1"));
    expect(api.runWorkflow).not.toHaveBeenCalled();
    expect(api.stepWorkflow).not.toHaveBeenCalled();
  });

  it("shows a loading state while the retry request is in flight", async () => {
    vi.mocked(api.getWorkflow).mockResolvedValue(makeWorkflow({ status: "failed" }));
    let resolveRetry: () => void = () => {};
    vi.mocked(api.retryWorkflow).mockImplementation(
      () => new Promise((resolve) => (resolveRetry = () => resolve(makeWorkflow({ status: "pending" })))),
    );
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "↻ Retry" }));
    await userEvent.click(screen.getByRole("button", { name: "Retry workflow" }));

    expect(screen.getByRole("button", { name: /working/i })).toBeDisabled();
    resolveRetry();
    // Several unrelated empty-state panels on this page also use role="status"
    // (see components/States), so match on the success text directly rather
    // than a role query, which would be ambiguous here.
    await waitFor(() => expect(screen.getByText(/✓ Done/)).toBeInTheDocument());
  });

  it("refreshes workflow state after a successful retry, reflecting PENDING/original stage", async () => {
    vi.mocked(api.getWorkflow)
      .mockResolvedValueOnce(makeWorkflow({ status: "failed", current_stage: "codegen" }))
      .mockResolvedValueOnce(makeWorkflow({ status: "pending", current_stage: "codegen" }));
    vi.mocked(api.retryWorkflow).mockResolvedValue(makeWorkflow({ status: "pending", current_stage: "codegen" }));
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "↻ Retry" }));
    await userEvent.click(screen.getByRole("button", { name: "Retry workflow" }));

    await waitFor(() => expect(api.getWorkflow).toHaveBeenCalledTimes(2));
  });
});
