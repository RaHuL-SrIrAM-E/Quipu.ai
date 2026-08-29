import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "./App";

vi.mock("./api/client", () => ({
  api: {
    listWorkflows: vi.fn().mockResolvedValue([]),
    listSignals: vi.fn().mockResolvedValue([]),
    listDetections: vi.fn().mockResolvedValue([]),
    listFeatureReviews: vi.fn().mockResolvedValue([]),
    listResolutions: vi.fn().mockResolvedValue([]),
    listVerifications: vi.fn().mockResolvedValue([]),
  },
  getReviewerId: vi.fn(() => ""),
  setReviewerId: vi.fn(),
}));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("App routing", () => {
  it("renders the Overview command center at /", async () => {
    renderAt("/");
    expect(await screen.findByRole("heading", { name: "Command Center" })).toBeInTheDocument();
  });

  it("has no route for arbitrary tool/shell/deploy execution — unknown paths fall back to Overview", async () => {
    for (const dangerousPath of ["/tools/execute", "/shell", "/deploy", "/agents/run", "/admin"]) {
      renderAt(dangerousPath);
      expect(await screen.findByRole("heading", { name: "Command Center" })).toBeInTheDocument();
      cleanup();
    }
  });

  it("exposes only the documented navigation items", () => {
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Primary" });
    const labels = Array.from(nav.querySelectorAll("a")).map((a) => a.textContent);
    expect(labels).toEqual(["Overview", "Workflows", "Signals", "Detections", "Feature Reviews", "Incidents", "Verifications"]);
  });
});
