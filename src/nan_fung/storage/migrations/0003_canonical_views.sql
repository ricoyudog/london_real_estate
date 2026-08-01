CREATE INDEX workflow_job_claim_idx
    ON workflow_job(state, available_at, priority, scheduled_for, job_id);
CREATE INDEX workflow_attempt_job_idx ON workflow_attempt(job_id, attempt_no);
CREATE INDEX evidence_artifact_content_idx ON evidence_artifact(content_sha256);
CREATE INDEX observation_stream_idx
    ON observation_revision(datasource_id, lane, record_key_version, record_key_hash, revision_no DESC);
CREATE INDEX run_observation_observation_idx ON run_observation(observation_id, run_id);
CREATE INDEX run_promotion_run_idx ON run_promotion(run_id, decision_at DESC, promotion_seq DESC);
CREATE INDEX audit_event_target_idx ON audit_event(target_type, target_id, created_at);

CREATE VIEW current_run_promotion_v1 AS
WITH ranked AS (
    SELECT p.*, row_number() OVER (
        PARTITION BY p.run_id
        ORDER BY p.decision_at DESC, p.promotion_seq DESC
    ) AS decision_rank
    FROM run_promotion AS p
)
SELECT promotion_seq, promotion_id, run_id, decision, approval_mode, decision_at,
       actor_type, actor_id, policy_version, reason, details_json
FROM ranked
WHERE decision_rank = 1;

CREATE VIEW canonical_event_v1 AS
SELECT ro.run_id AS canonical_run_id,
       CASE WHEN p.decision_at > a.completed_at THEN p.decision_at ELSE a.completed_at END AS available_at,
       a.completed_at AS run_completed_at,
       r.definition_version AS seen_under_definition_version,
       r.definition_hash AS seen_under_definition_hash,
       o.observation_id, o.datasource_id, o.definition_version,
       o.record_key_version, o.record_key_json, o.record_key_hash,
       o.snapshot_scope_hash, o.revision_no, o.revision_action, o.revision_reason,
       o.record_hash, o.category, o.record_type, o.payload_json, o.source_date,
       o.period_start, o.period_end, o.period_label, o.geography_code,
       o.geography_name, o.unit, o.data_kind, o.confidence, o.definition,
       o.limitations_json, o.parser_version, o.schema_version, o.supersedes_id,
       o.created_at
FROM run_observation AS ro
JOIN ingestion_run AS r ON r.run_id = ro.run_id
JOIN workflow_attempt AS a ON a.attempt_id = r.attempt_id
JOIN observation_revision AS o ON o.observation_id = ro.observation_id
JOIN current_run_promotion_v1 AS p ON p.run_id = r.run_id
WHERE r.lane = 'production_ingestion'
  AND a.status = 'succeeded'
  AND o.lane = 'production_ingestion'
  AND p.decision = 'approved';

CREATE VIEW canonical_latest_v1 AS
WITH ranked AS (
    SELECT ce.*, row_number() OVER (
        PARTITION BY ce.datasource_id, ce.record_key_version, ce.record_key_hash
        ORDER BY ce.revision_no DESC, ce.run_completed_at DESC, ce.canonical_run_id DESC
    ) AS record_rank
    FROM canonical_event_v1 AS ce
)
SELECT canonical_run_id, available_at, run_completed_at,
       seen_under_definition_version, seen_under_definition_hash, observation_id,
       datasource_id, definition_version, record_key_version, record_key_json,
       record_key_hash, snapshot_scope_hash, revision_no, revision_action,
       revision_reason, record_hash, category, record_type, payload_json,
       source_date, period_start, period_end, period_label, geography_code,
       geography_name, unit, data_kind, confidence, definition, limitations_json,
       parser_version, schema_version, supersedes_id, created_at
FROM ranked
WHERE record_rank = 1 AND revision_action = 'upsert';
