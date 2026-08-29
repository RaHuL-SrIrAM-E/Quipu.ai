import { useState } from "react";
import { ApiError } from "../api/client";

type Phase = "idle" | "confirm" | "running" | "success" | "error";

/**
 * A privileged command (approve/reject/step/remediate) never fires on a
 * single click — see docs/architecture/control_plane_ui.md "Command UX".
 * First click asks for confirmation with an explicit description of what
 * will happen; the second click actually calls the API. Loading/success/
 * failure are always shown, never silent.
 */
export function CommandButton({
  label,
  confirmLabel,
  description,
  tone = "primary",
  onRun,
  onSuccess,
}: {
  label: string;
  confirmLabel: string;
  description: string;
  tone?: "primary" | "danger" | "ghost";
  onRun: () => Promise<unknown>;
  onSuccess?: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  if (phase === "confirm") {
    return (
      <div className="command-confirm" role="alertdialog" aria-label={`Confirm: ${label}`}>
        <p>{description}</p>
        <div className="command-confirm-actions">
          <button
            type="button"
            className={`btn btn-${tone}`}
            autoFocus
            onClick={async () => {
              setPhase("running");
              setError(null);
              try {
                await onRun();
                setPhase("success");
                onSuccess?.();
              } catch (err) {
                setError(err instanceof ApiError ? err.message : "The request failed. Please try again.");
                setPhase("error");
              }
            }}
          >
            {confirmLabel}
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => setPhase("idle")}>
            Cancel
          </button>
        </div>
      </div>
    );
  }

  if (phase === "running") {
    return (
      <button type="button" className={`btn btn-${tone}`} disabled aria-busy="true">
        <span className="spinner spinner-inline" aria-hidden="true" /> Working…
      </button>
    );
  }

  if (phase === "success") {
    return (
      <span className="command-result command-success" role="status">
        ✓ Done
      </span>
    );
  }

  if (phase === "error") {
    return (
      <div className="command-result command-error" role="alert">
        <span>✕ {error}</span>
        <button type="button" className="btn btn-ghost" onClick={() => setPhase("confirm")}>
          Try again
        </button>
      </div>
    );
  }

  return (
    <button type="button" className={`btn btn-${tone}`} onClick={() => setPhase("confirm")}>
      {label}
    </button>
  );
}
