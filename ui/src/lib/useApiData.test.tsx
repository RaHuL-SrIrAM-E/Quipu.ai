import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useApiData } from "./useApiData";

function Probe({ fetcher, pollMs }: { fetcher: () => Promise<string>; pollMs?: number }) {
  const { data, loading } = useApiData(fetcher, [], pollMs);
  return <div>{loading && data === null ? "loading" : `data:${data}`}</div>;
}

describe("useApiData", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("fetches once on mount", async () => {
    const fetcher = vi.fn().mockResolvedValue("v1");
    await act(async () => {
      render(<Probe fetcher={fetcher} />);
    });
    expect(fetcher).toHaveBeenCalledOnce();
    expect(screen.getByText("data:v1")).toBeInTheDocument();
  });

  it("polls again after the interval while the tab is visible", async () => {
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    const fetcher = vi.fn().mockResolvedValue("v1");
    await act(async () => {
      render(<Probe fetcher={fetcher} pollMs={5000} />);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("does not poll while the tab is hidden", async () => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    const fetcher = vi.fn().mockResolvedValue("v1");
    await act(async () => {
      render(<Probe fetcher={fetcher} pollMs={5000} />);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(20000);
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("never polls when no interval is given", async () => {
    const fetcher = vi.fn().mockResolvedValue("v1");
    await act(async () => {
      render(<Probe fetcher={fetcher} />);
    });
    await act(async () => {
      vi.advanceTimersByTime(60000);
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
