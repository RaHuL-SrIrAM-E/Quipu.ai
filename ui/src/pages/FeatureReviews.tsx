import { api, getReviewerId } from "../api/client";
import { useApiData } from "../lib/useApiData";
import { PageHeader, Panel } from "../components/Layout";
import { DataView } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { CommandButton } from "../components/CommandButton";
import { formatDateTime } from "../lib/format";
import type { FeatureReviewSummary } from "../api/types";

function ReviewCard({ review, onChanged }: { review: FeatureReviewSummary; onChanged: () => void }) {
  const detection = useApiData(() => api.getDetection(review.detection_id), [review.detection_id]);
  const reviewerId = getReviewerId();

  return (
    <article className="review-card">
      <div className="review-flow">
        <span>AI detected</span>
        <span aria-hidden="true">→</span>
        <span className={review.status === "pending" ? "review-flow-current" : ""}>Human reviews</span>
        <span aria-hidden="true">→</span>
        <span className={review.status === "approved" ? "review-flow-current" : ""}>Engineering workflow</span>
      </div>

      {detection.data && (
        <>
          <h3>{detection.data.title}</h3>
          <p>{detection.data.summary}</p>
          <p className="muted">{detection.data.rationale}</p>
          <dl className="kv-grid">
            <dt>Confidence</dt>
            <dd>{Math.round(detection.data.confidence * 100)}%</dd>
            <dt>Supporting signals</dt>
            <dd>{detection.data.supporting_signal_ids.length}</dd>
            <dt>Subject</dt>
            <dd>{detection.data.subject}</dd>
          </dl>
        </>
      )}

      <div className="review-card-footer">
        <StatusBadge status={review.status} />
        {review.reviewer_id && (
          <span className="muted">
            {review.status === "approved" ? "Approved" : "Rejected"} by {review.reviewer_id} · {formatDateTime(review.reviewed_at)}
          </span>
        )}
      </div>

      {review.status === "pending" && (
        <div className="review-card-actions">
          {!reviewerId && <p className="muted">Set a reviewer identity (top right) before approving or rejecting.</p>}
          <CommandButton
            label="Approve"
            confirmLabel="Approve & create engineering ticket"
            description="This authorizes FeatureReviewService to create a Jira ticket and makes this opportunity eligible to start an engineering workflow. An agent can never approve its own detection — this requires a human reviewer identity."
            onRun={() => api.approveFeatureReview(review.review_id)}
            onSuccess={onChanged}
          />
          <CommandButton
            label="Reject"
            confirmLabel="Reject opportunity"
            description="This permanently marks the opportunity as rejected. No ticket is created."
            tone="danger"
            onRun={() => api.rejectFeatureReview(review.review_id)}
            onSuccess={onChanged}
          />
        </div>
      )}

      {review.ticket_title && (
        <p className="muted">
          Ticket: {review.ticket_title} ({review.ticket_id})
        </p>
      )}
    </article>
  );
}

export function FeatureReviews() {
  const reviews = useApiData(() => api.listFeatureReviews({ limit: 100 }), [], 10_000);

  return (
    <>
      <PageHeader
        title="Feature Review Queue"
        description="The human-in-the-loop boundary: Detecting Agent proposes a product opportunity, a human reviewer approves or rejects it — Quipu never starts engineering work on its own."
      />
      <Panel>
        <DataView loading={reviews.loading} error={reviews.error} data={reviews.data} onRetry={reviews.refresh} isEmpty={(d) => d.length === 0} emptyMessage="No feature opportunities awaiting review.">
          {(data) => (
            <div className="review-grid">
              {data.map((r) => (
                <ReviewCard key={r.review_id} review={r} onChanged={reviews.refresh} />
              ))}
            </div>
          )}
        </DataView>
      </Panel>
    </>
  );
}
