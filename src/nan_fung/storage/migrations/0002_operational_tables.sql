-- The operational schema deliberately uses a shared provenance envelope.  The
-- typed projection tables below are derived convenience indexes, never a
-- second source of truth.

CREATE TABLE datasource_definition (
    datasource_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL CHECK(definition_version > 0),
    definition_hash TEXT NOT NULL CHECK(length(definition_hash) = 64),
    display_name TEXT NOT NULL,
    publisher TEXT NOT NULL,
    category TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    automation_mode TEXT NOT NULL,
    snapshot_mode TEXT NOT NULL,
    default_lane TEXT NOT NULL CHECK(default_lane IN ('production_ingestion', 'source_discovery', 'ad_hoc_research')),
    promotion_policy TEXT NOT NULL,
    data_kind TEXT NOT NULL,
    default_confidence TEXT NOT NULL,
    collector_name TEXT NOT NULL,
    collector_version TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    record_key_builder_name TEXT NOT NULL,
    record_key_version TEXT NOT NULL,
    locator_version TEXT NOT NULL,
    allowed_hosts_json TEXT NOT NULL CHECK(json_valid(allowed_hosts_json)),
    validation_policy_json TEXT NOT NULL CHECK(json_valid(validation_policy_json)),
    retry_policy_json TEXT NOT NULL CHECK(json_valid(retry_policy_json)),
    timeout_policy_json TEXT NOT NULL CHECK(json_valid(timeout_policy_json)),
    artifact_policy_json TEXT NOT NULL CHECK(json_valid(artifact_policy_json)),
    freshness_policy_json TEXT NOT NULL CHECK(json_valid(freshness_policy_json)),
    capabilities_json TEXT NOT NULL CHECK(json_valid(capabilities_json)),
    licence TEXT,
    access_class TEXT NOT NULL CHECK(access_class IN ('open', 'internal', 'restricted', 'reference_only')),
    retention_policy TEXT NOT NULL,
    definition_json TEXT NOT NULL CHECK(json_valid(definition_json)),
    status TEXT NOT NULL CHECK(status IN ('draft', 'discovery', 'production', 'retired')),
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(datasource_id, definition_version),
    UNIQUE(definition_hash),
    CHECK(status != 'production' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
) STRICT;

CREATE TABLE source_definition (
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL CHECK(source_version > 0),
    source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),
    display_name TEXT NOT NULL,
    publisher TEXT NOT NULL,
    surface_kind TEXT NOT NULL,
    base_origin_redacted TEXT,
    allowed_hosts_json TEXT NOT NULL CHECK(json_valid(allowed_hosts_json)),
    licence TEXT,
    access_class TEXT NOT NULL CHECK(access_class IN ('open', 'internal', 'restricted', 'reference_only')),
    retention_profile TEXT NOT NULL,
    source_json TEXT NOT NULL CHECK(json_valid(source_json)),
    status TEXT NOT NULL CHECK(status IN ('draft', 'discovery', 'production', 'retired')),
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_id, source_version),
    UNIQUE(source_hash),
    CHECK(status != 'production' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
) STRICT;

CREATE TABLE datasource_source (
    datasource_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    role TEXT NOT NULL,
    required INTEGER NOT NULL CHECK(required IN (0, 1)),
    PRIMARY KEY(datasource_id, definition_version, source_id, source_version),
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version),
    FOREIGN KEY(source_id, source_version)
        REFERENCES source_definition(source_id, source_version)
) STRICT;

CREATE TABLE workflow_schedule (
    schedule_id TEXT PRIMARY KEY,
    task_kind TEXT NOT NULL,
    datasource_id TEXT,
    definition_version INTEGER,
    name TEXT NOT NULL,
    lane TEXT CHECK(lane IN ('production_ingestion', 'source_discovery', 'ad_hoc_research')),
    rule_json TEXT NOT NULL CHECK(json_valid(rule_json)),
    rule_hash TEXT NOT NULL CHECK(length(rule_hash) = 64),
    timezone TEXT NOT NULL,
    catchup_policy TEXT NOT NULL CHECK(catchup_policy IN ('latest_only', 'windowed', 'all_slots', 'manual')),
    max_catchup_jobs INTEGER NOT NULL CHECK(max_catchup_jobs BETWEEN 1 AND 1000),
    max_catchup_horizon_seconds INTEGER NOT NULL CHECK(max_catchup_horizon_seconds >= 0),
    cursor_at TEXT,
    next_due_at TEXT,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    paused_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version),
    UNIQUE(task_kind, datasource_id, definition_version, name, rule_hash)
) STRICT;

CREATE TABLE workflow_job (
    job_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE CHECK(length(dedupe_key) = 64),
    job_kind TEXT NOT NULL,
    datasource_id TEXT,
    definition_version INTEGER,
    definition_hash TEXT CHECK(definition_hash IS NULL OR length(definition_hash) = 64),
    schedule_id TEXT,
    parent_job_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    request_instance_id TEXT,
    lane TEXT CHECK(lane IN ('production_ingestion', 'source_discovery', 'ad_hoc_research')),
    trigger TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    available_at TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    as_of_at TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    state TEXT NOT NULL CHECK(state IN ('queued', 'claimed', 'running', 'retry_wait', 'succeeded', 'empty', 'failed', 'dead_letter', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 20),
    claim_token TEXT,
    claimed_by TEXT,
    claimed_at TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    last_error_json TEXT CHECK(last_error_json IS NULL OR json_valid(last_error_json)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancel_requested_at TEXT,
    cancel_requested_by TEXT,
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version),
    FOREIGN KEY(schedule_id) REFERENCES workflow_schedule(schedule_id),
    FOREIGN KEY(parent_job_id) REFERENCES workflow_job(job_id),
    CHECK(attempt_count <= max_attempts),
    CHECK(window_end IS NULL OR window_start IS NOT NULL),
    CHECK(window_start IS NULL OR window_end IS NULL OR window_start <= window_end)
) STRICT;

CREATE TABLE workflow_attempt (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'empty', 'partial', 'failed', 'cancelled')),
    worker_id TEXT NOT NULL,
    trace_id TEXT,
    session_id TEXT,
    warnings_json TEXT NOT NULL CHECK(json_valid(warnings_json)),
    error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(job_id) REFERENCES workflow_job(job_id),
    UNIQUE(job_id, attempt_no),
    CHECK((status = 'running' AND completed_at IS NULL) OR (status != 'running' AND completed_at IS NOT NULL))
) STRICT;

CREATE TABLE ingestion_run (
    run_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL,
    datasource_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    definition_hash TEXT NOT NULL CHECK(length(definition_hash) = 64),
    lane TEXT NOT NULL CHECK(lane IN ('production_ingestion', 'source_discovery', 'ad_hoc_research')),
    trigger TEXT NOT NULL,
    requested_as_of_at TEXT,
    retrieved_at TEXT,
    record_count INTEGER NOT NULL DEFAULT 0 CHECK(record_count >= 0),
    accepted_record_count INTEGER NOT NULL DEFAULT 0 CHECK(accepted_record_count >= 0),
    rejected_record_count INTEGER NOT NULL DEFAULT 0 CHECK(rejected_record_count >= 0),
    snapshot_complete INTEGER NOT NULL DEFAULT 0 CHECK(snapshot_complete IN (0, 1)),
    snapshot_scope_json TEXT CHECK(snapshot_scope_json IS NULL OR json_valid(snapshot_scope_json)),
    snapshot_scope_hash TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'empty', 'partial', 'failed', 'cancelled')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(attempt_id) REFERENCES workflow_attempt(attempt_id),
    FOREIGN KEY(job_id) REFERENCES workflow_job(job_id),
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version)
) STRICT;

CREATE TABLE content_object (
    content_sha256 TEXT PRIMARY KEY CHECK(length(content_sha256) = 64),
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    artifact_uri TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    verified_at TEXT NOT NULL
) STRICT;

CREATE TABLE evidence_artifact (
    evidence_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    media_type TEXT,
    access_class TEXT NOT NULL CHECK(access_class IN ('open', 'internal', 'restricted', 'reference_only')),
    retention_until TEXT,
    retrieved_at TEXT NOT NULL,
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    source_id TEXT,
    source_version INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(content_sha256) REFERENCES content_object(content_sha256),
    FOREIGN KEY(source_id, source_version) REFERENCES source_definition(source_id, source_version)
) STRICT;

CREATE TABLE acquisition_event (
    acquisition_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    evidence_id TEXT,
    role TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed', 'cancelled')),
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
    FOREIGN KEY(run_id) REFERENCES ingestion_run(run_id),
    FOREIGN KEY(evidence_id) REFERENCES evidence_artifact(evidence_id)
) STRICT;

CREATE TABLE run_evidence (
    run_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    role TEXT NOT NULL,
    required INTEGER NOT NULL CHECK(required IN (0, 1)),
    discovered_by_evidence_id TEXT,
    PRIMARY KEY(run_id, evidence_id, role),
    FOREIGN KEY(run_id) REFERENCES ingestion_run(run_id),
    FOREIGN KEY(evidence_id) REFERENCES evidence_artifact(evidence_id),
    FOREIGN KEY(discovered_by_evidence_id) REFERENCES evidence_artifact(evidence_id)
) STRICT;

CREATE TABLE observation_revision (
    observation_id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    lane TEXT NOT NULL CHECK(lane IN ('production_ingestion', 'source_discovery', 'ad_hoc_research')),
    record_key_version TEXT NOT NULL,
    record_key_json TEXT NOT NULL CHECK(json_valid(record_key_json)),
    record_key_hash TEXT NOT NULL CHECK(length(record_key_hash) = 64),
    snapshot_scope_hash TEXT,
    revision_no INTEGER NOT NULL CHECK(revision_no > 0),
    revision_action TEXT NOT NULL CHECK(revision_action IN ('upsert', 'tombstone')),
    revision_reason TEXT NOT NULL,
    record_hash TEXT NOT NULL CHECK(length(record_hash) = 64),
    category TEXT NOT NULL,
    record_type TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    source_date TEXT,
    period_start TEXT,
    period_end TEXT,
    period_label TEXT,
    geography_code TEXT,
    geography_name TEXT,
    unit TEXT,
    data_kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    definition TEXT,
    limitations_json TEXT NOT NULL CHECK(json_valid(limitations_json)),
    parser_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    supersedes_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version),
    FOREIGN KEY(supersedes_id) REFERENCES observation_revision(observation_id),
    UNIQUE(datasource_id, lane, record_key_version, record_key_hash, revision_no)
) STRICT;

CREATE TABLE record_stream_head (
    datasource_id TEXT NOT NULL,
    lane TEXT NOT NULL CHECK(lane IN ('production_ingestion', 'source_discovery', 'ad_hoc_research')),
    record_key_version TEXT NOT NULL,
    record_key_hash TEXT NOT NULL CHECK(length(record_key_hash) = 64),
    observation_id TEXT NOT NULL,
    record_hash TEXT NOT NULL CHECK(length(record_hash) = 64),
    revision_no INTEGER NOT NULL,
    revision_action TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(datasource_id, lane, record_key_version, record_key_hash),
    FOREIGN KEY(observation_id) REFERENCES observation_revision(observation_id)
) STRICT;

CREATE TABLE run_observation (
    run_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    PRIMARY KEY(run_id, observation_id),
    FOREIGN KEY(run_id) REFERENCES ingestion_run(run_id),
    FOREIGN KEY(observation_id) REFERENCES observation_revision(observation_id)
) STRICT;

CREATE TABLE observation_evidence (
    run_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
    locator_hash TEXT NOT NULL CHECK(length(locator_hash) = 64),
    PRIMARY KEY(run_id, observation_id, evidence_id, locator_hash),
    FOREIGN KEY(run_id, observation_id) REFERENCES run_observation(run_id, observation_id),
    FOREIGN KEY(evidence_id) REFERENCES evidence_artifact(evidence_id)
) STRICT;

CREATE TABLE data_quality_issue (
    issue_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    observation_id TEXT,
    severity TEXT NOT NULL CHECK(severity IN ('warning', 'error')),
    code TEXT NOT NULL,
    details_json TEXT NOT NULL CHECK(json_valid(details_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES ingestion_run(run_id),
    FOREIGN KEY(observation_id) REFERENCES observation_revision(observation_id)
) STRICT;

CREATE TABLE run_promotion (
    promotion_id TEXT PRIMARY KEY,
    promotion_seq INTEGER NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected', 'revoked')),
    approval_mode TEXT NOT NULL CHECK(approval_mode IN ('automatic', 'manual')),
    decision_at TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    reason TEXT,
    details_json TEXT NOT NULL CHECK(json_valid(details_json)),
    FOREIGN KEY(run_id) REFERENCES ingestion_run(run_id)
) STRICT;

CREATE TABLE review_task (
    review_id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    run_id TEXT,
    task_type TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'approved', 'rejected', 'cancelled')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version),
    FOREIGN KEY(run_id) REFERENCES ingestion_run(run_id)
) STRICT;

CREATE TABLE extraction_proposal (
    proposal_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    proposal_json TEXT NOT NULL CHECK(json_valid(proposal_json)),
    model_metadata_json TEXT NOT NULL CHECK(json_valid(model_metadata_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(review_id) REFERENCES review_task(review_id)
) STRICT;

CREATE TABLE review_decision (
    decision_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
    actor_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(review_id) REFERENCES review_task(review_id)
) STRICT;

CREATE TABLE metric_value (
    observation_id TEXT PRIMARY KEY,
    metric_name TEXT NOT NULL,
    numeric_value REAL,
    numeric_text TEXT,
    FOREIGN KEY(observation_id) REFERENCES observation_revision(observation_id),
    CHECK(numeric_value IS NOT NULL OR numeric_text IS NOT NULL)
) STRICT;

CREATE TABLE supply_project (
    observation_id TEXT PRIMARY KEY,
    project_name TEXT,
    status TEXT,
    expected_completion_date TEXT,
    FOREIGN KEY(observation_id) REFERENCES observation_revision(observation_id)
) STRICT;

CREATE TABLE market_event (
    observation_id TEXT PRIMARY KEY,
    event_type TEXT,
    event_at TEXT,
    relevance_status TEXT,
    FOREIGN KEY(observation_id) REFERENCES observation_revision(observation_id)
) STRICT;

CREATE TABLE geography (
    observation_id TEXT PRIMARY KEY,
    geometry_json TEXT CHECK(geometry_json IS NULL OR json_valid(geometry_json)),
    srid INTEGER,
    FOREIGN KEY(observation_id) REFERENCES observation_revision(observation_id)
) STRICT;

CREATE TABLE submarket_definition (
    submarket_version_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL CHECK(json_valid(definition_json)),
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved', 'retired')),
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    CHECK(status != 'approved' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
) STRICT;

CREATE TABLE location_submarket_mapping (
    mapping_id TEXT PRIMARY KEY,
    submarket_version_id TEXT NOT NULL,
    location_key TEXT NOT NULL,
    mapping_json TEXT NOT NULL CHECK(json_valid(mapping_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(submarket_version_id) REFERENCES submarket_definition(submarket_version_id)
) STRICT;

CREATE TABLE output_artifact (
    output_id TEXT PRIMARY KEY,
    output_type TEXT NOT NULL,
    path TEXT NOT NULL,
    source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),
    details_json TEXT NOT NULL CHECK(json_valid(details_json)),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE audit_event (
    audit_id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details_json TEXT NOT NULL CHECK(json_valid(details_json)),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE operational_alert (
    alert_id TEXT PRIMARY KEY,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'acknowledged', 'resolved')),
    details_json TEXT NOT NULL CHECK(json_valid(details_json)),
    created_at TEXT NOT NULL,
    resolved_at TEXT
) STRICT;

CREATE TABLE backup_set (
    backup_id TEXT PRIMARY KEY,
    database_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    verified_at TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE service_heartbeat (
    instance_id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('daemon', 'worker')),
    app_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('starting', 'running', 'stopping', 'failed')),
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    details_json TEXT NOT NULL CHECK(json_valid(details_json))
) STRICT;

CREATE TABLE host_throttle (
    rate_limit_group TEXT PRIMARY KEY,
    next_allowed_at TEXT,
    blocked_until TEXT,
    last_http_status INTEGER,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE source_watermark (
    datasource_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    lane TEXT NOT NULL CHECK(lane IN ('production_ingestion', 'source_discovery', 'ad_hoc_research')),
    stream_key TEXT NOT NULL,
    watermark_json TEXT NOT NULL CHECK(json_valid(watermark_json)),
    watermark_hash TEXT NOT NULL CHECK(length(watermark_hash) = 64),
    advanced_by_run_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(datasource_id, definition_version, lane, stream_key),
    FOREIGN KEY(datasource_id, definition_version)
        REFERENCES datasource_definition(datasource_id, definition_version),
    FOREIGN KEY(advanced_by_run_id) REFERENCES ingestion_run(run_id)
) STRICT;
