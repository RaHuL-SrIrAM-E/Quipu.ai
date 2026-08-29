import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";

export interface ApiDataState<T> {
  data: T | null;
  loading: boolean;
  error: ApiError | Error | null;
  refresh: () => void;
}

/**
 * Fetches `fetcher()` on mount and whenever `deps` change, with optional
 * lightweight polling. Polling pauses when the browser tab is hidden
 * (document.visibilitychange) so an inactive tab never hammers the API —
 * see docs/architecture/control_plane_ui.md "Polling".
 */
export function useApiData<T>(fetcher: () => Promise<T>, deps: unknown[], pollIntervalMs?: number): ApiDataState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(async (isBackground: boolean) => {
    if (!isBackground) setLoading(true);
    try {
      const result = await fetcherRef.current();
      setData(result);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (!isBackground) setLoading(false);
    }
  }, []);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    if (!pollIntervalMs) return;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      if (document.visibilityState === "visible") void load(true);
    };
    const id = window.setInterval(tick, pollIntervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollIntervalMs, ...deps]);

  const refresh = useCallback(() => void load(false), [load]);

  return { data, loading, error, refresh };
}
