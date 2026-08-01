from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from nan_fung.storage.db import (
    MigrationChecksumError,
    MigrationRunner,
    backup_database,
    connect_database,
    integrity_check,
)


def test_write_and_read_connections_apply_the_runtime_policy(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    writer = connect_database(database)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert writer.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert writer.execute("PRAGMA synchronous").fetchone()[0] == 2
        writer.execute("CREATE TABLE sample (value TEXT) STRICT")
        writer.execute("INSERT INTO sample (value) VALUES ('saved')")
    finally:
        writer.close()

    reader = connect_database(database, read_only=True)
    try:
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        assert reader.execute("SELECT value FROM sample").fetchone()[0] == "saved"
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO sample (value) VALUES ('blocked')")
    finally:
        reader.close()


def test_packaged_migrations_are_idempotent_and_recorded(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    runner = MigrationRunner(database, app_version="test-version")

    applied = runner.migrate()

    assert [migration.filename for migration in applied] == [
        "0001_schema_migration.sql",
        "0002_operational_tables.sql",
        "0003_canonical_views.sql",
        "0004_manual_promotion.sql",
        "0005_append_only_guards.sql",
        "0006_refresh_request_ledger.sql",
        "0007_refresh_confirmation.sql",
    ]
    assert runner.migrate() == ()
    assert runner.validate() == ()
    connection = connect_database(database, read_only=True)
    try:
        row = connection.execute(
            "SELECT version, name, checksum_sha256, app_version FROM schema_migration"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == (1, "schema_migration", applied[0].checksum_sha256, "test-version")


def test_append_only_guards_reject_direct_lineage_rewrites(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    MigrationRunner(database).migrate()
    connection = connect_database(database)
    try:
        connection.execute(
            """
            INSERT INTO audit_event (
                audit_id, actor_type, actor_id, action, target_type, target_id,
                details_json, created_at
            ) VALUES ('audit_test', 'operator', 'tester', 'created', 'test', 'target', '{}', '2026-08-01T00:00:00Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_AUDIT_EVENT"):
            connection.execute(
                "UPDATE audit_event SET action = 'rewritten' WHERE audit_id = 'audit_test'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_AUDIT_EVENT"):
            connection.execute("DELETE FROM audit_event WHERE audit_id = 'audit_test'")
        assert connection.execute(
            "SELECT action FROM audit_event WHERE audit_id = 'audit_test'"
        ).fetchone()[0] == "created"
        connection.execute(
            """
            INSERT INTO output_artifact (
                output_id, output_type, path, source_hash, details_json, created_at
            ) VALUES ('out_test', 'wiki', 'wiki/market.md', ?, '{}', '2026-08-01T00:00:00Z')
            """,
            ("0" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_OUTPUT_ARTIFACT"):
            connection.execute("DELETE FROM output_artifact WHERE output_id = 'out_test'")
    finally:
        connection.close()


def test_migration_runner_rejects_a_changed_applied_file(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    migration = migrations / "0001_example.sql"
    migration.write_text("CREATE TABLE example (value TEXT) STRICT;\n", encoding="utf-8")

    MigrationRunner(database, migration_directory=migrations).migrate()
    migration.write_text("CREATE TABLE example (value TEXT NOT NULL) STRICT;\n", encoding="utf-8")

    with pytest.raises(MigrationChecksumError, match="checksum"):
        MigrationRunner(database, migration_directory=migrations).migrate()


def test_failed_migration_rolls_back_its_schema_changes(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_broken.sql").write_text(
        "CREATE TABLE should_rollback (value TEXT) STRICT;\nTHIS IS NOT SQL;\n",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.OperationalError):
        MigrationRunner(database, migration_directory=migrations).migrate()

    connection = connect_database(database, read_only=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        ledger_count = connection.execute("SELECT count(*) FROM schema_migration").fetchone()[0]
    finally:
        connection.close()
    assert "should_rollback" not in tables
    assert ledger_count == 0


def test_integrity_check_and_online_backup_are_usable_offline(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    MigrationRunner(database).migrate()
    connection = connect_database(database)
    try:
        connection.execute("CREATE TABLE sample (value TEXT) STRICT")
        connection.execute("INSERT INTO sample (value) VALUES ('backup me')")
    finally:
        connection.close()

    backup = backup_database(database, tmp_path / "backups" / "state.sqlite")

    assert integrity_check(database).ok
    assert integrity_check(backup).ok
    backup_connection = connect_database(backup, read_only=True)
    try:
        assert backup_connection.execute("SELECT value FROM sample").fetchone()[0] == "backup me"
    finally:
        backup_connection.close()
