from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nan_fung.backups import BackupError, create_backup_set, restore_backup_set, verify_backup_set
from nan_fung.ingestion.bank_rate import AcquiredArtifact
from nan_fung.operational import OperationalStore
from nan_fung.storage.artifacts import ArtifactStore
from nan_fung.workflows import ingest_bank_rate_artifact


def test_backup_set_and_restore_include_database_and_evidence(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path / "source")
    artifact = AcquiredArtifact(
        body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
        source_url="https://www.bankofengland.co.uk/data.csv",
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    result = ingest_bank_rate_artifact(store, artifact)

    backup = create_backup_set(store, tmp_path / "backup")
    checked = verify_backup_set(backup.directory)
    restored = restore_backup_set(backup.directory, tmp_path / "restored")

    assert checked.content_count == 1
    assert restored.verified is True
    assert OperationalStore(tmp_path / "restored").read_evidence(result.evidence_id) == artifact.body


def test_backup_does_not_overwrite_target(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path / "source")
    store.migrate()
    target = tmp_path / "backup"
    target.mkdir()

    with pytest.raises(FileExistsError):
        create_backup_set(store, target)


@pytest.mark.parametrize("target_name", ("operational.sqlite3", "manifest.json"))
def test_backup_verify_rejects_a_symlinked_top_level_file(
    tmp_path: Path, target_name: str
) -> None:
    store = OperationalStore(tmp_path / "source")
    store.migrate()
    backup = create_backup_set(store, tmp_path / "backup")
    target = backup.directory / target_name
    external = tmp_path / f"external-{target_name}"
    target.replace(external)
    target.symlink_to(external)

    with pytest.raises(BackupError, match="regular file"):
        verify_backup_set(backup.directory)


def test_backup_verify_rejects_a_symlinked_referenced_cas_object(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path / "source")
    ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
            source_url="https://www.bankofengland.co.uk/data.csv",
            retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )
    backup = create_backup_set(store, tmp_path / "backup")
    artifacts = ArtifactStore(backup.directory)
    digest = artifacts.published_digests()[0]
    target = artifacts.object_path(digest)
    external = tmp_path / "external-evidence"
    target.replace(external)
    target.symlink_to(external)

    with pytest.raises(BackupError, match="regular file"):
        verify_backup_set(backup.directory)
