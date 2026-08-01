"""Local verified SQLite-plus-CAS backup sets and safe restore drills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4

from nan_fung.ingestion.canonical import canonical_json, new_id
from nan_fung.operational import OperationalStore
from nan_fung.storage.artifacts import ArtifactStore
from nan_fung.storage.db import backup_database, connect_database, integrity_check, transaction


class BackupError(RuntimeError):
    """A backup set is incomplete, corrupt, or unsafe to restore."""


@dataclass(frozen=True)
class BackupSetReport:
    schema_version: str
    directory: Path
    database_sha256: str
    content_count: int
    verified: bool

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "directory": str(self.directory),
            "database_sha256": self.database_sha256,
            "content_count": self.content_count,
            "verified": self.verified,
        }


def create_backup_set(store: OperationalStore, directory: str | Path) -> BackupSetReport:
    """Create a complete local set; an existing target is never overwritten."""

    with store.writer_session():
        store.migrate()
        return _create_backup_set(store, directory)


def create_pre_migration_backup(
    store: OperationalStore, backup_dir: str | Path
) -> BackupSetReport:
    """Create a verified snapshot without changing the source database."""

    with store.writer_session():
        target = Path(backup_dir).expanduser().resolve() / f"pre-migration-{uuid4().hex}"
        return _create_backup_set(store, target, record=False)


def _create_backup_set(
    store: OperationalStore, directory: str | Path, *, record: bool = True
) -> BackupSetReport:
    """Create one backup while the caller holds the store writer lease."""

    target = Path(directory).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"backup set target already exists: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        staging.mkdir(mode=0o700)
        database = staging / "operational.sqlite3"
        backup_database(store.database_path, database)
        content = _referenced_content(database)
        source_artifacts = store.artifacts
        destination_artifacts = ArtifactStore(staging)
        for digest, byte_size in content:
            if not source_artifacts.verify(digest):
                raise BackupError(f"source CAS object is missing or corrupt: {digest}")
            source = source_artifacts.object_path(digest)
            destination = destination_artifacts.object_path(digest)
            _copy_regular_file(source, destination)
            if not destination_artifacts.verify(digest):
                raise BackupError(f"copied CAS object failed verification: {digest}")
            if destination.stat().st_size != byte_size:
                raise BackupError(f"copied CAS object size changed: {digest}")
        manifest = {
            "schema_version": "backup_set.v1",
            "created_at": _timestamp(),
            "database": {
                "path": "operational.sqlite3",
                "sha256": _file_hash(database),
            },
            "content": [
                {"sha256": digest, "byte_size": byte_size} for digest, byte_size in content
            ],
        }
        _write_atomic(staging / "manifest.json", canonical_json(manifest).encode("utf-8"))
        report = verify_backup_set(staging)
        os.replace(staging, target)
        _fsync_directory(target.parent)
        report = BackupSetReport(
            report.schema_version,
            target,
            report.database_sha256,
            report.content_count,
            report.verified,
        )
        if record:
            _record_backup_set(store, report)
        return report
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_backup_set(directory: str | Path) -> BackupSetReport:
    """Verify a set's SQLite integrity and every DB-referenced CAS object."""

    supplied_root = Path(directory).expanduser()
    _require_regular_directory(supplied_root, "root")
    root = supplied_root.resolve()
    manifest_path = root / "manifest.json"
    database = root / "operational.sqlite3"
    _require_regular_file(manifest_path, "manifest.json")
    _require_regular_file(database, "operational.sqlite3")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError("backup manifest is unreadable") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "backup_set.v1":
        raise BackupError("backup manifest schema is unsupported")
    database_info = manifest.get("database")
    if not isinstance(database_info, dict) or database_info.get("path") != "operational.sqlite3":
        raise BackupError("backup manifest database entry is invalid")
    digest = _file_hash(database)
    if digest != database_info.get("sha256"):
        raise BackupError("backup database hash does not match manifest")
    report = integrity_check(database)
    if not report.ok:
        raise BackupError("backup database integrity check failed")
    expected = _referenced_content(database)
    manifest_content = manifest.get("content")
    if not isinstance(manifest_content, list):
        raise BackupError("backup manifest content entry is invalid")
    manifest_pairs = []
    for item in manifest_content:
        if not isinstance(item, dict) or not isinstance(item.get("sha256"), str):
            raise BackupError("backup manifest content entry is invalid")
        manifest_pairs.append((item["sha256"], item.get("byte_size")))
    if manifest_pairs != [(digest, size) for digest, size in expected]:
        raise BackupError("backup manifest content does not match database")
    if expected:
        _require_regular_directory(root / "evidence", "evidence directory")
        _require_regular_directory(root / "evidence" / "sha256", "CAS directory")
    artifacts = ArtifactStore(root)
    for content_digest, byte_size in expected:
        _require_regular_directory(
            root / "evidence" / "sha256" / content_digest[:2],
            "CAS prefix directory",
        )
        _require_regular_file(
            artifacts.object_path(content_digest), "CAS object"
        )
        if not artifacts.verify(content_digest):
            raise BackupError(f"backup CAS object failed verification: {content_digest}")
        if artifacts.object_path(content_digest).stat().st_size != byte_size:
            raise BackupError(f"backup CAS object size changed: {content_digest}")
    return BackupSetReport("backup_set.v1", root, digest, len(expected), True)


def restore_backup_set(
    source: str | Path, target_data_dir: str | Path
) -> BackupSetReport:
    """Restore a verified backup only into a previously non-existent directory."""

    source_report = verify_backup_set(Path(source).expanduser())
    source_root = source_report.directory
    target = Path(target_data_dir).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"restore target already exists: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        staging.mkdir(mode=0o700)
        _copy_regular_file(source_root / "operational.sqlite3", staging / "operational.sqlite3")
        _copy_regular_file(source_root / "manifest.json", staging / "manifest.json")
        for digest, _ in _referenced_content(source_root / "operational.sqlite3"):
            _copy_regular_file(
                ArtifactStore(source_root).object_path(digest),
                ArtifactStore(staging).object_path(digest),
            )
        verified = verify_backup_set(staging)
        os.replace(staging, target)
        _fsync_directory(target.parent)
        return BackupSetReport(
            verified.schema_version,
            target,
            verified.database_sha256,
            verified.content_count,
            verified.verified,
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _referenced_content(database: Path) -> tuple[tuple[str, int], ...]:
    connection = connect_database(database, read_only=True)
    try:
        rows = connection.execute(
            "SELECT content_sha256, byte_size FROM content_object ORDER BY content_sha256"
        ).fetchall()
        return tuple((row["content_sha256"], row["byte_size"]) for row in rows)
    finally:
        connection.close()


def _record_backup_set(store: OperationalStore, report: BackupSetReport) -> None:
    connection = connect_database(store.database_path)
    now = _timestamp()
    manifest = {
        "schema_version": report.schema_version,
        "database_sha256": report.database_sha256,
        "content_count": report.content_count,
        "verified": report.verified,
    }
    try:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO backup_set (backup_id, database_path, manifest_json, verified_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (new_id("backup"), str(report.directory), canonical_json(manifest), now, now),
            )
    finally:
        connection.close()


def _copy_regular_file(source: Path, destination: Path) -> None:
    try:
        source_stat = source.lstat()
    except FileNotFoundError as error:
        raise BackupError(f"backup input is missing: {source}") from error
    if not source.is_file() or source.is_symlink():
        raise BackupError(f"backup input is not a regular file: {source}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        raise BackupError(f"backup destination already exists: {destination}")
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_file, temporary.open("xb", buffering=0) as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _require_regular_file(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as error:
        raise BackupError(f"backup set is missing {label}") from error
    if not stat.S_ISREG(details.st_mode):
        raise BackupError(f"backup {label} must be a regular file")


def _require_regular_directory(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as error:
        raise BackupError(f"backup set is missing {label}") from error
    if not stat.S_ISDIR(details.st_mode):
        raise BackupError(f"backup {label} must be a regular directory")


def _write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb", buffering=0) as output_file:
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _file_hash(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
