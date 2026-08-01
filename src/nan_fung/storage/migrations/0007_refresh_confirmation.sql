-- A competition-scoped second confirmation for refreshes that exceed a
-- datasource's durable daily job budget.  The row is append-only evidence of
-- the first intent; the linked refresh_request records the later accepted job.

CREATE TABLE refresh_confirmation (
    confirmation_token TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    principal TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    datasource_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    day_start_at TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL CHECK(expires_at > issued_at),
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version)
) STRICT;

CREATE INDEX refresh_confirmation_expiry_idx
    ON refresh_confirmation(expires_at);

CREATE TRIGGER refresh_confirmation_no_update
BEFORE UPDATE ON refresh_confirmation BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_REFRESH_CONFIRMATION');
END;

CREATE TRIGGER refresh_confirmation_no_delete
BEFORE DELETE ON refresh_confirmation BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_REFRESH_CONFIRMATION');
END;
