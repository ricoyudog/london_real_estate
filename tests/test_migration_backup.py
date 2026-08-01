from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from nan_fung.backups import verify_backup_set
from nan_fung.operational import OperationalError, OperationalStore
from nan_fung.storage.artifacts import ArtifactStore
from nan_fung.storage.db import MigrationRunner, connect_database


def test_fresh_database_migrates_without_a_backup_directory(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path / "data")

    assert store.migrate()
    assert MigrationRunner(store.database_path).validate() == ()


def test_pending_migration_requires_a_configured_backup_before_mutating(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    pending_filename, _ = _seed_database_one_migration_behind(data_dir, tmp_path)
    store = OperationalStore(data_dir)

    with pytest.raises(OperationalError, match="configured backup_dir"):
        store.migrate()

    assert [migration.filename for migration in MigrationRunner(store.database_path).validate()] == [
        pending_filename
    ]


def test_pending_migration_creates_a_verified_database_and_cas_backup(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    pending_filename, digest = _seed_database_one_migration_behind(data_dir, tmp_path)
    backup_dir = tmp_path / "backups"
    store = OperationalStore(data_dir, backup_dir=backup_dir)

    assert store.migrate() == (pending_filename,)

    backup_sets = tuple(path for path in backup_dir.iterdir() if path.is_dir())
    assert len(backup_sets) == 1
    report = verify_backup_set(backup_sets[0])
    assert report.verified is True
    assert report.content_count == 1
    assert ArtifactStore(report.directory).verify(digest)
    assert [
        migration.filename
        for migration in MigrationRunner(report.directory / "operational.sqlite3").validate()
    ] == [pending_filename]
    assert MigrationRunner(store.database_path).validate() == ()


def _seed_database_one_migration_behind(data_dir: Path, tmp_path: Path) -> tuple[str, str]:
    migration_directory = tmp_path / "previous-migrations"
    migration_directory.mkdir()
    packaged = resources.files("nan_fung.storage.migrations")
    filenames = tuple(sorted(entry.name for entry in packaged.iterdir() if entry.name.endswith(".sql")))
    assert len(filenames) > 1
    for filename in filenames[:-1]:
        (migration_directory / filename).write_bytes(packaged.joinpath(filename).read_bytes())
    MigrationRunner(
        data_dir / "operational.sqlite3", migration_directory=migration_directory
    ).migrate()

    artifact = ArtifactStore(data_dir).put_bytes(b"pre-migration backup evidence")
    connection = connect_database(data_dir / "operational.sqlite3")
    try:
        connection.execute(
            """
            INSERT INTO content_object (
                content_sha256, byte_size, artifact_uri, created_at, verified_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                artifact.content_sha256,
                artifact.byte_size,
                artifact.artifact_uri,
                "2026-08-01T00:00:00.000000Z",
                "2026-08-01T00:00:00.000000Z",
            ),
        )
    finally:
        connection.close()
    return filenames[-1], artifact.content_sha256
