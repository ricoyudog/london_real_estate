-- A host-only approval is a durable capability, not a bearer confirmation
-- token.  The existing refresh_confirmation table remains the token authority;
-- this mapping deliberately stores only the immutable request identity needed
-- to recover it after a facade restart.

CREATE TABLE agent_refresh_approval (
    approval_id TEXT PRIMARY KEY,
    refresh_request_id TEXT NOT NULL UNIQUE,
    principal TEXT NOT NULL,
    capability_scope_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    manifest_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    request_snapshot_json TEXT NOT NULL CHECK(json_valid(request_snapshot_json)),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL CHECK(expires_at > issued_at),
    FOREIGN KEY(refresh_request_id) REFERENCES refresh_confirmation(request_id)
) STRICT;

CREATE INDEX agent_refresh_approval_scope_idx
    ON agent_refresh_approval(principal, capability_scope_id, expires_at);

CREATE TABLE agent_refresh_approval_event (
    event_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK(event_seq > 0),
    event_type TEXT NOT NULL CHECK(event_type IN ('decision', 'replay')),
    decision TEXT NOT NULL CHECK(decision IN ('approve', 'deny')),
    actor_type TEXT NOT NULL CHECK(actor_type = 'host'),
    actor_id TEXT NOT NULL,
    details_json TEXT NOT NULL CHECK(json_valid(details_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(approval_id) REFERENCES agent_refresh_approval(approval_id),
    UNIQUE(approval_id, event_seq)
) STRICT;

CREATE INDEX agent_refresh_approval_event_lookup_idx
    ON agent_refresh_approval_event(approval_id, event_seq);

CREATE TRIGGER agent_refresh_approval_no_update
BEFORE UPDATE ON agent_refresh_approval BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_AGENT_REFRESH_APPROVAL');
END;

CREATE TRIGGER agent_refresh_approval_no_delete
BEFORE DELETE ON agent_refresh_approval BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_AGENT_REFRESH_APPROVAL');
END;

CREATE TRIGGER agent_refresh_approval_event_no_update
BEFORE UPDATE ON agent_refresh_approval_event BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_AGENT_REFRESH_APPROVAL_EVENT');
END;

CREATE TRIGGER agent_refresh_approval_event_no_delete
BEFORE DELETE ON agent_refresh_approval_event BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_AGENT_REFRESH_APPROVAL_EVENT');
END;

CREATE TRIGGER agent_refresh_approval_event_sequence
BEFORE INSERT ON agent_refresh_approval_event
WHEN NEW.event_seq != COALESCE(
    (SELECT MAX(event_seq) + 1
     FROM agent_refresh_approval_event
     WHERE approval_id = NEW.approval_id),
    1
)
BEGIN
    SELECT RAISE(ABORT, 'AGENT_REFRESH_APPROVAL_EVENT_SEQUENCE_INVALID');
END;

CREATE TRIGGER agent_refresh_approval_event_one_decision
BEFORE INSERT ON agent_refresh_approval_event
WHEN NEW.event_type = 'decision'
 AND EXISTS (
    SELECT 1 FROM agent_refresh_approval_event
    WHERE approval_id = NEW.approval_id AND event_type = 'decision'
 )
BEGIN
    SELECT RAISE(ABORT, 'AGENT_REFRESH_APPROVAL_ALREADY_DECIDED');
END;

CREATE TRIGGER agent_refresh_approval_event_replay_matches_decision
BEFORE INSERT ON agent_refresh_approval_event
WHEN NEW.event_type = 'replay'
 AND NOT EXISTS (
    SELECT 1 FROM agent_refresh_approval_event
    WHERE approval_id = NEW.approval_id
      AND event_type = 'decision'
      AND decision = NEW.decision
 )
BEGIN
    SELECT RAISE(ABORT, 'AGENT_REFRESH_APPROVAL_REPLAY_MISMATCH');
END;
