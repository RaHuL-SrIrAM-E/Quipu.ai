import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CommandButton } from "./CommandButton";

describe("CommandButton", () => {
  it("never fires the command on a single click — it asks for confirmation first", async () => {
    const onRun = vi.fn().mockResolvedValue(undefined);
    render(<CommandButton label="Approve" confirmLabel="Approve & create ticket" description="This authorizes a Jira ticket." onRun={onRun} />);

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByText("This authorizes a Jira ticket.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Approve & create ticket" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledOnce());
  });

  it("shows a loading state while the command is in flight, then success", async () => {
    let resolveRun: () => void = () => {};
    const onRun = vi.fn(() => new Promise<void>((resolve) => (resolveRun = resolve)));
    render(<CommandButton label="Run step" confirmLabel="Run" description="advances the workflow" onRun={onRun} />);

    await userEvent.click(screen.getByRole("button", { name: "Run step" }));
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(screen.getByRole("button", { name: /working/i })).toBeDisabled();
    resolveRun();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/done/i));
  });

  it("shows a failure state and allows retry when the command rejects", async () => {
    const onRun = vi.fn().mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce(undefined);
    render(<CommandButton label="Reject" confirmLabel="Reject" description="rejects the opportunity" tone="danger" onRun={onRun} />);

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/done/i));
  });

  it("cancel returns to idle without ever calling the command", async () => {
    const onRun = vi.fn();
    render(<CommandButton label="Authorize" confirmLabel="Authorize remediation" description="authorizes remediation" onRun={onRun} />);
    await userEvent.click(screen.getByRole("button", { name: "Authorize" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Authorize" })).toBeInTheDocument();
    expect(onRun).not.toHaveBeenCalled();
  });
});
