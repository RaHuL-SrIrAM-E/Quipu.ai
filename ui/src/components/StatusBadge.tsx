// Status is always communicated by a text label, not color alone — see
// docs/architecture/control_plane_ui.md "Accessibility".

const TONE_BY_STATUS: Record<string, string> = {
  completed: "good",
  verified_resolved: "good",
  approved: "good",
  succeeded: "good",
  healthy: "good",
  running: "active",
  pending: "active",
  in_progress: "active",
  waiting: "active",
  blocked: "warn",
  still_degraded: "warn",
  insufficient_evidence: "warn",
  no_action: "neutral",
  escalated: "danger",
  failed: "danger",
  rejected: "danger",
  cancelled: "neutral",
  degraded: "warn",
};

function toneFor(status: string): string {
  return TONE_BY_STATUS[status.toLowerCase()] ?? "neutral";
}

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const tone = toneFor(status);
  const text = label ?? status.replace(/_/g, " ");
  return (
    <span className={`badge badge-${tone}`} role="status">
      <span className="badge-dot" aria-hidden="true" />
      {text}
    </span>
  );
}

export function DomainBadge({ domain }: { domain: string }) {
  const tone = domain === "operational" ? "operational" : "product";
  return (
    <span className={`domain-badge domain-${tone}`} role="status">
      {domain === "operational" ? "Operational" : "Product"}
    </span>
  );
}
