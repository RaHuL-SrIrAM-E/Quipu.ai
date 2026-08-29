import type { ReactNode } from "react";
import { ApiError } from "../api/client";

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state-panel state-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="state-panel state-empty" role="status">
      {children}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  const isApiError = error instanceof ApiError;
  return (
    <div className="state-panel state-error" role="alert">
      <p>{isApiError ? error.message : "Unable to reach the Control Plane API."}</p>
      {isApiError && error.correlationId && <p className="state-error-meta">correlation id: {error.correlationId}</p>}
      {onRetry && (
        <button type="button" className="btn btn-ghost" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** Renders loading/error/empty/content uniformly so no page ever shows a
 * blank screen — see docs/architecture/control_plane_ui.md "Error/empty/
 * loading states". */
export function DataView<T>({
  loading,
  error,
  data,
  onRetry,
  isEmpty,
  emptyMessage,
  children,
}: {
  loading: boolean;
  error: Error | null;
  data: T | null;
  onRetry?: () => void;
  isEmpty: (data: T) => boolean;
  emptyMessage: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (loading && data === null) return <LoadingState />;
  if (error) return <ErrorState error={error} onRetry={onRetry} />;
  if (data === null) return <LoadingState />;
  if (isEmpty(data)) return <EmptyState>{emptyMessage}</EmptyState>;
  return <>{children(data)}</>;
}
