import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FeatureReviews } from "./FeatureReviews";
import type { DetectionSummary, FeatureReviewSummary } from "../api/types";

const review: FeatureReviewSummary = {
  review_id: "review-1",
  detection_id: "detection-1",
  status: "pending",
  reviewer_id: null,
  reviewer_type: null,
  review_comment: null,
  reviewed_at: null,
  ticket_id: null,
  ticket_title: null,
  workflow_id: null,
  created_at: "2026-01-01T00:00:00Z",
};

const detection: DetectionSummary = {
  detection_id: "detection-1",
  detection_type: "feature_opportunity",
  domain: "product",
  title: "Add CSV export",
  summary: "Multiple customers requested CSV export.",
  rationale: "Two independent signals converge.",
  confidence: 0.85,
  severity: null,
  subject: "reporting",
  service_name: null,
  environment: null,
  supporting_signal_ids: ["s1", "s2"],
  knowledge_references: [],
  observation_window_minutes: 10080,
  detected_at: "2026-01-01T00:00:00Z",
};

vi.mock("../api/client", () => ({
  api: {
    listFeatureReviews: vi.fn(),
    getDetection: vi.fn(),
    approveFeatureReview: vi.fn(),
    rejectFeatureReview: vi.fn(),
  },
  getReviewerId: vi.fn(() => "alice"),
}));

import { api } from "../api/client";

function renderPage() {
  return render(
    <MemoryRouter>
      <FeatureReviews />
    </MemoryRouter>,
  );
}

describe("FeatureReviews page", () => {
  beforeEach(() => {
    vi.mocked(api.listFeatureReviews).mockResolvedValue([review]);
    vi.mocked(api.getDetection).mockResolvedValue(detection);
  });

  it("shows the AI detected -> human reviews -> engineering workflow story with evidence", async () => {
    renderPage();
    expect(await screen.findByText("Add CSV export")).toBeInTheDocument();
    expect(screen.getByText("Human reviews")).toBeInTheDocument();
    expect(screen.getByText("85%")).toBeInTheDocument();
  });

  it("approves through the Control Plane API and reflects the result, never reimplementing authorization client-side", async () => {
    vi.mocked(api.approveFeatureReview).mockResolvedValue({ ...review, status: "approved", reviewer_id: "alice", reviewer_type: "human" });
    renderPage();
    await screen.findByText("Add CSV export");

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: /approve & create/i }));

    await waitFor(() => expect(api.approveFeatureReview).toHaveBeenCalledWith("review-1"));
  });

  it("rejects through the Control Plane API", async () => {
    vi.mocked(api.rejectFeatureReview).mockResolvedValue({ ...review, status: "rejected", reviewer_id: "alice" });
    renderPage();
    await screen.findByText("Add CSV export");

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await userEvent.click(screen.getByRole("button", { name: "Reject opportunity" }));

    await waitFor(() => expect(api.rejectFeatureReview).toHaveBeenCalledWith("review-1"));
  });

  it("shows an empty state when there is nothing to review", async () => {
    vi.mocked(api.listFeatureReviews).mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/no feature opportunities awaiting review/i)).toBeInTheDocument();
  });
});
