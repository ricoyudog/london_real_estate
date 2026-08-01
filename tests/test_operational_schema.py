from __future__ import annotations

import json
from pathlib import Path

from nan_fung.storage.db import MigrationRunner, connect_database
from nan_fung.read_api.access import ReadContext
from nan_fung.read_api.contracts import ReadQuery
from nan_fung.read_api.sqlite_repository import SQLiteReadRepository


def test_operational_migrations_create_provenance_tables_and_canonical_view(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    MigrationRunner(database).migrate()
    connection = connect_database(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "datasource_definition",
            "workflow_job",
            "workflow_attempt",
            "ingestion_run",
            "evidence_artifact",
            "observation_revision",
            "run_promotion",
            "manual_review_promotion",
            "refresh_request",
            "audit_event",
        } <= tables

        _insert_definition(connection)
        _insert_canonical_run(connection)

        row = connection.execute(
            "SELECT datasource_id, observation_id, payload_json FROM canonical_latest_v1"
        ).fetchone()
        assert tuple(row) == (
            "boe.bank_rate.iudbedr",
            "obs_rate_1",
            '{"bank_rate_percent":"3.75"}',
        )
    finally:
        connection.close()


def test_sqlite_read_repository_uses_as_of_canonical_history(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    MigrationRunner(database).migrate()
    connection = connect_database(database)
    try:
        _insert_definition(connection)
        _insert_canonical_run(connection)
    finally:
        connection.close()

    records = tuple(
        SQLiteReadRepository(database).query_canonical(
            ReadQuery(query_kind="metrics"),
            as_of=_timestamp(),
            context=ReadContext("dashboard", frozenset({"open"})),
        )
    )

    assert len(records) == 1
    assert records[0].observation_id == "obs_rate_1"
    assert records[0].payload["bank_rate_percent"] == "3.75"


def _timestamp():
    from datetime import UTC, datetime

    return datetime(2026, 8, 1, tzinfo=UTC)


def _insert_definition(connection: object) -> None:
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO datasource_definition (
            datasource_id, definition_version, definition_hash, display_name,
            publisher, category, source_kind, automation_mode, snapshot_mode,
            default_lane, promotion_policy, data_kind, default_confidence,
            collector_name, collector_version, parser_name, parser_version,
            schema_version, record_key_builder_name, record_key_version,
            locator_version, allowed_hosts_json, validation_policy_json,
            retry_policy_json, timeout_policy_json, artifact_policy_json,
            freshness_policy_json, capabilities_json, licence, access_class,
            retention_policy, definition_json, status, approved_by, approved_at,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "boe.bank_rate.iudbedr",
            1,
            "a" * 64,
            "Bank Rate",
            "Bank of England",
            "macro",
            "structured_api",
            "automatic",
            "append_only",
            "production_ingestion",
            "automatic",
            "direct",
            "high",
            "bank_rate",
            "v1",
            "bank_rate",
            "v1",
            "v1",
            "bank_rate_key",
            "v1",
            "v1",
            "[]",
            "{}",
            "{}",
            "{}",
            "{}",
            "{}",
            "{}",
            "OGL",
            "open",
            "project-lifetime",
            "{}",
            "production",
            "operator",
            "2026-08-01T00:00:00.000000Z",
            "2026-08-01T00:00:00.000000Z",
        ),
    )


def _insert_canonical_run(connection: object) -> None:
    timestamp = "2026-08-01T00:00:00.000000Z"
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO workflow_job (
            job_id, dedupe_key, job_kind, datasource_id, definition_version,
            definition_hash, lane, trigger, scheduled_for, available_at,
            request_json, request_hash, state, max_attempts, created_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "job_rate_1",
            "b" * 64,
            "scheduled_ingest",
            "boe.bank_rate.iudbedr",
            1,
            "a" * 64,
            "production_ingestion",
            "schedule",
            timestamp,
            timestamp,
            "{}",
            "c" * 64,
            "succeeded",
            4,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO workflow_attempt (
            attempt_id, job_id, attempt_no, status, worker_id, warnings_json,
            started_at, heartbeat_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("attempt_rate_1", "job_rate_1", 1, "succeeded", "worker", "[]", timestamp, timestamp, timestamp),
    )
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO ingestion_run (
            run_id, attempt_id, job_id, datasource_id, definition_version,
            definition_hash, lane, trigger, status, created_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run_rate_1",
            "attempt_rate_1",
            "job_rate_1",
            "boe.bank_rate.iudbedr",
            1,
            "a" * 64,
            "production_ingestion",
            "schedule",
            "succeeded",
            timestamp,
            timestamp,
        ),
    )
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO observation_revision (
            observation_id, datasource_id, definition_version, lane,
            record_key_version, record_key_json, record_key_hash, revision_no,
            revision_action, revision_reason, record_hash, category, record_type,
            payload_json, data_kind, confidence, limitations_json, parser_version,
            schema_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "obs_rate_1",
            "boe.bank_rate.iudbedr",
            1,
            "production_ingestion",
            "v1",
            '["IUDBEDR","2026-07-30"]',
            "d" * 64,
            1,
            "upsert",
            "first_seen",
            "e" * 64,
            "macro",
            "metric",
            json.dumps({"bank_rate_percent": "3.75"}, separators=(",", ":")),
            "direct",
            "high",
            "[]",
            "v1",
            "v1",
            timestamp,
        ),
    )
    connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO run_observation (run_id, observation_id) VALUES (?, ?)",
        ("run_rate_1", "obs_rate_1"),
    )
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO run_promotion (
            promotion_id, promotion_seq, run_id, decision, approval_mode,
            decision_at, actor_type, actor_id, policy_version, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "promotion_rate_1",
            1,
            "run_rate_1",
            "approved",
            "automatic",
            timestamp,
            "service",
            "daemon",
            "v1",
            "{}",
        ),
    )
