-- A manual promotion is an immutable link from one approved human review to
-- one explicit run-promotion decision.  The links make retries idempotent and
-- preserve the decision lineage independently of the current canonical view.

CREATE TABLE manual_review_promotion (
    review_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL UNIQUE,
    promotion_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(review_id) REFERENCES review_task(review_id),
    FOREIGN KEY(decision_id) REFERENCES review_decision(decision_id),
    FOREIGN KEY(run_id) REFERENCES ingestion_run(run_id),
    FOREIGN KEY(promotion_id) REFERENCES run_promotion(promotion_id)
) STRICT;

CREATE INDEX manual_review_promotion_run_idx
    ON manual_review_promotion(run_id, created_at);
