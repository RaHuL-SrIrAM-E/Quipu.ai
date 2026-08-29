import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StageTimeline } from "./StageTimeline";

describe("StageTimeline", () => {
  it("marks stages before the current one as done and after as upcoming", () => {
    render(<StageTimeline currentStage="testing" status="running" />);
    expect(screen.getByText("Planning").closest("li")).toHaveClass("stage-done");
    expect(screen.getByText("Testing").closest("li")).toHaveClass("stage-active");
    expect(screen.getByText("Deployment").closest("li")).toHaveClass("stage-upcoming");
  });

  it("marks the current stage as failed when workflow status is failed", () => {
    render(<StageTimeline currentStage="codegen" status="failed" />);
    expect(screen.getByText("Codegen").closest("li")).toHaveClass("stage-failed");
  });

  it("renders a distinct Verification step reflecting remediation outcome, never conflated with deployment", () => {
    render(<StageTimeline currentStage="completed" status="completed" remediationOutcome="deployed_pending_verification" />);
    const verificationStep = screen.getByText("Verification").closest("li")!;
    expect(verificationStep).toHaveTextContent("deployed pending verification");
    expect(verificationStep).not.toHaveClass("stage-done");
  });

  it("renders the Verification step as done only for verified_resolved", () => {
    render(<StageTimeline currentStage="completed" status="completed" remediationOutcome="verified_resolved" />);
    expect(screen.getByText("Verification").closest("li")).toHaveClass("stage-done");
  });

  it("omits the Verification step entirely when there is no remediation outcome", () => {
    render(<StageTimeline currentStage="planning" status="pending" />);
    expect(screen.queryByText("Verification")).not.toBeInTheDocument();
  });
});
