import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DataView } from "./States";
import { ApiError } from "../api/client";

describe("DataView", () => {
  it("shows a loading state while data is null", () => {
    render(<DataView loading data={null} error={null} isEmpty={() => false} emptyMessage="empty">{() => <div />}</DataView>);
    expect(screen.getByRole("status")).toHaveTextContent(/loading/i);
  });

  it("shows a safe error message, never the raw exception internals", () => {
    const err = new ApiError(500, { error: "internal_error", detail: "an internal error occurred", correlation_id: "corr-1" });
    render(<DataView loading={false} data={null} error={err} isEmpty={() => false} emptyMessage="empty">{() => <div />}</DataView>);
    expect(screen.getByRole("alert")).toHaveTextContent("an internal error occurred");
    expect(screen.getByText(/corr-1/)).toBeInTheDocument();
  });

  it("calls onRetry when Retry is clicked", async () => {
    const onRetry = vi.fn();
    const err = new ApiError(500, { error: "internal_error", detail: "failed", correlation_id: null });
    render(<DataView loading={false} data={null} error={err} onRetry={onRetry} isEmpty={() => false} emptyMessage="empty">{() => <div />}</DataView>);
    screen.getByRole("button", { name: /retry/i }).click();
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("shows the caller-provided empty message when data is empty", () => {
    render(
      <DataView loading={false} data={[]} error={null} isEmpty={(d: unknown[]) => d.length === 0} emptyMessage="No active workflows.">
        {() => <div>should not render</div>}
      </DataView>,
    );
    expect(screen.getByText("No active workflows.")).toBeInTheDocument();
    expect(screen.queryByText("should not render")).not.toBeInTheDocument();
  });

  it("renders children with the data once loaded and non-empty", () => {
    render(
      <DataView loading={false} data={["x"]} error={null} isEmpty={(d: unknown[]) => d.length === 0} emptyMessage="empty">
        {(data) => <div>count: {data.length}</div>}
      </DataView>,
    );
    expect(screen.getByText("count: 1")).toBeInTheDocument();
  });
});
