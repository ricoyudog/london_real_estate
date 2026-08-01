-- Agent refresh idempotency and status visibility survive broker restarts.
-- Rows are append-only: a request ID is permanently bound to its principal,
-- fingerprint, dedupe cohort, and durable workflow job.

CREATE TABLE refresh_request (
    request_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    dedupe_key TEXT NOT NULL CHECK(length(dedupe_key) = 64),
    datasource_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    request_profile TEXT NOT NULL,
    job_id TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN ('accepted', 'deduplicated')),
    initial_state TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    cooldown_until TEXT NOT NULL CHECK(cooldown_until >= submitted_at),
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version),
    FOREIGN KEY(job_id) REFERENCES workflow_job(job_id)
) STRICT;

CREATE INDEX refresh_request_dedupe_cooldown_idx
    ON refresh_request(dedupe_key, cooldown_until DESC, submitted_at DESC);

CREATE INDEX refresh_request_job_principal_idx
    ON refresh_request(job_id, principal);

CREATE TRIGGER refresh_request_no_update
BEFORE UPDATE ON refresh_request BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_REFRESH_REQUEST');
END;

CREATE TRIGGER refresh_request_no_delete
BEFORE DELETE ON refresh_request BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_REFRESH_REQUEST');
END;
