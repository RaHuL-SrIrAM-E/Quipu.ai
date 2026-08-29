import { useState } from "react";
import { getReviewerId, setReviewerId } from "../api/client";

/**
 * The development-mode attribution identity used for feature-review
 * approve/reject calls (X-Quipu-Reviewer-Id). This is explicitly NOT an
 * authentication credential — it grants no privilege by itself, only
 * attributes an approve/reject action to a name. See
 * docs/architecture/control_plane_ui.md "Authentication boundary" for the
 * honest statement of what this is and is not.
 */
export function ReviewerIdentityControl() {
  const [value, setValue] = useState(getReviewerId());
  const [editing, setEditing] = useState(false);

  if (!editing) {
    return (
      <button type="button" className="reviewer-chip" onClick={() => setEditing(true)} title="Development-mode attribution identity — not a login">
        {value ? `Acting as ${value}` : "Set reviewer identity"}
      </button>
    );
  }

  return (
    <form
      className="reviewer-form"
      onSubmit={(e) => {
        e.preventDefault();
        setReviewerId(value.trim());
        setEditing(false);
      }}
    >
      <label htmlFor="reviewer-id" className="sr-only">
        Reviewer identity (attribution only)
      </label>
      <input id="reviewer-id" value={value} onChange={(e) => setValue(e.target.value)} placeholder="your name" autoFocus />
      <button type="submit" className="btn btn-ghost btn-small">
        Save
      </button>
    </form>
  );
}
