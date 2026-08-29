import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, getReviewerId, setReviewerId } from "./client";

describe("api client", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("builds bounded query strings and returns parsed JSON on success", async () => {
    const mockFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([{ workflow_id: "wf-1" }]), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    globalThis.fetch = mockFetch as unknown as typeof fetch;

    await api.listWorkflows({ status: "completed", limit: 10 });

    const calledUrl = mockFetch.mock.calls[0][0] as string;
    expect(calledUrl).toContain("/workflows?");
    expect(calledUrl).toContain("status=completed");
    expect(calledUrl).toContain("limit=10");
  });

  it("throws ApiError with the server's error/detail/correlation_id on non-2xx", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: "not_found", detail: "WorkflowState 'x' not found", correlation_id: "abc-123" }), { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(api.getWorkflow("x")).rejects.toMatchObject({
      status: 404,
      code: "not_found",
      correlationId: "abc-123",
    });
  });

  it("wraps a non-JSON failure response safely rather than throwing a raw parse error", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("<html>502</html>", { status: 502 })) as unknown as typeof fetch;

    await expect(api.getWorkflow("x")).rejects.toBeInstanceOf(ApiError);
  });

  it("sends the reviewer identity header only on command routes, never a privilege flag", async () => {
    setReviewerId("alice");
    const mockFetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "approved" }), { status: 200 }));
    globalThis.fetch = mockFetch as unknown as typeof fetch;

    await api.approveFeatureReview("review-1", "looks good");

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/feature-reviews/review-1/approve");
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Quipu-Reviewer-Id"]).toBe("alice");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ review_comment: "looks good" });
    expect(body).not.toHaveProperty("reviewer_type");
    expect(body).not.toHaveProperty("is_admin");
  });

  it("getReviewerId defaults to empty when nothing is stored", () => {
    expect(getReviewerId()).toBe("");
  });
});
