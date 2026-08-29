// The ONLY module in this app allowed to call `fetch` against the Control
// Plane API. Every page/component goes through the typed functions below
// — never raw fetch calls scattered across components — see
// docs/architecture/control_plane_ui.md "Security model".
//
// No Google credentials, Firestore access, or any secret lives here or
// anywhere else in this app: the browser talks exclusively to the
// Control Plane API (app/api/), which is itself the only thing that ever
// talks to Firestore/Gemini/Pub/Sub/Cloud Run.

import type {
  ApiErrorResponse,
  ArtifactSummary,
  DecisionSummary,
  DetectionSummary,
  ExecutionSummary,
  FeatureReviewSummary,
  ResolutionSummary,
  SignalDetail,
  SignalSummary,
  VerificationSummary,
  WorkflowDetail,
  WorkflowSummary,
} from "./types";

// Configurable at build/deploy time — see docs/architecture/control_plane_ui.md
// "Local development" / "Cloud Run deployment". Empty string means
// same-origin (the UI is served by the same Cloud Run service as the API).
const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "";

// Development-mode attribution identity ONLY — never a privilege claim.
// See app/api/auth.py and docs/architecture/control_plane_api.md
// "Authorization boundary". This is intentionally readable/editable by
// the operator running the UI locally, not a secret.
const REVIEWER_STORAGE_KEY = "quipu.reviewerId";

export function getReviewerId(): string {
  try {
    return localStorage.getItem(REVIEWER_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setReviewerId(value: string): void {
  try {
    localStorage.setItem(REVIEWER_STORAGE_KEY, value);
  } catch {
    // localStorage unavailable (private browsing, etc.) — attribution
    // header simply won't be sent; the API will 401 on command routes.
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;

  constructor(status: number, body: ApiErrorResponse) {
    super(body.detail);
    this.status = status;
    this.code = body.error;
    this.correlationId = body.correlation_id;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let body: ApiErrorResponse;
    try {
      body = (await response.json()) as ApiErrorResponse;
    } catch {
      body = { error: "unknown_error", detail: `request failed with status ${response.status}`, correlation_id: null };
    }
    throw new ApiError(response.status, body);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function commandHeaders(): HeadersInit {
  const reviewerId = getReviewerId();
  return reviewerId ? { "X-Quipu-Reviewer-Id": reviewerId } : {};
}

function query(params: Record<string, string | number | undefined | null>): string {
  const usable = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (usable.length === 0) return "";
  const search = new URLSearchParams(usable.map(([k, v]) => [k, String(v)]));
  return `?${search.toString()}`;
}

export const api = {
  health: () => request<{ status: string }>("/health"),

  listWorkflows: (params: { status?: string; limit?: number } = {}) =>
    request<WorkflowSummary[]>(`/workflows${query(params)}`),
  getWorkflow: (workflowId: string) => request<WorkflowDetail>(`/workflows/${workflowId}`),
  listWorkflowArtifacts: (workflowId: string) => request<ArtifactSummary[]>(`/workflows/${workflowId}/artifacts`),
  listWorkflowExecutions: (workflowId: string) => request<ExecutionSummary[]>(`/workflows/${workflowId}/executions`),
  listWorkflowDecisions: (workflowId: string) => request<DecisionSummary[]>(`/workflows/${workflowId}/decisions`),
  stepWorkflow: (workflowId: string) =>
    request<WorkflowDetail>(`/workflows/${workflowId}/step`, { method: "POST", headers: commandHeaders() }),

  listSignals: (
    params: {
      signal_type?: string;
      source?: string;
      severity?: string;
      status?: string;
      service_name?: string;
      environment?: string;
      since?: string;
      until?: string;
      limit?: number;
    } = {},
  ) => request<SignalSummary[]>(`/signals${query(params)}`),
  getSignal: (signalId: string) => request<SignalDetail>(`/signals/${signalId}`),

  listDetections: (
    params: {
      detection_type?: string;
      domain?: string;
      service_name?: string;
      environment?: string;
      limit?: number;
    } = {},
  ) => request<DetectionSummary[]>(`/detections${query(params)}`),
  getDetection: (detectionId: string) => request<DetectionSummary>(`/detections/${detectionId}`),

  listResolutions: (params: { detection_id?: string; remediation_strategy?: string; risk?: string; limit?: number } = {}) =>
    request<ResolutionSummary[]>(`/resolutions${query(params)}`),
  getResolution: (resolutionId: string) => request<ResolutionSummary>(`/resolutions/${resolutionId}`),
  remediateResolution: (resolutionId: string) =>
    request<WorkflowDetail>(`/resolutions/${resolutionId}/remediate`, { method: "POST", headers: commandHeaders() }),

  listVerifications: (params: { outcome?: string; status?: string; limit?: number } = {}) =>
    request<VerificationSummary[]>(`/verifications${query(params)}`),
  getVerification: (verificationId: string) => request<VerificationSummary>(`/verifications/${verificationId}`),

  listFeatureReviews: (params: { limit?: number } = {}) => request<FeatureReviewSummary[]>(`/feature-reviews${query(params)}`),
  getFeatureReview: (reviewId: string) => request<FeatureReviewSummary>(`/feature-reviews/${reviewId}`),
  approveFeatureReview: (reviewId: string, comment?: string) =>
    request<FeatureReviewSummary>(`/feature-reviews/${reviewId}/approve`, {
      method: "POST",
      headers: commandHeaders(),
      body: JSON.stringify({ review_comment: comment ?? null }),
    }),
  rejectFeatureReview: (reviewId: string, comment?: string) =>
    request<FeatureReviewSummary>(`/feature-reviews/${reviewId}/reject`, {
      method: "POST",
      headers: commandHeaders(),
      body: JSON.stringify({ review_comment: comment ?? null }),
    }),
};
