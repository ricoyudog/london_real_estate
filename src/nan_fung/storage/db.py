"""SQLite connection, migration, integrity, and backup helpers.

Only the daemon or an explicitly exclusive offline operation should open a
write connection.  This module does not acquire the daemon's writer lock;
callers are responsible for that process-level ownership boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
import os
import re
import sqlite3
from typing import Iterator
from uuid import uuid4


MIGRATION_FILENAME = re.compile(r"^(?P<version>0*[1-9][0-9]*)_(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)\.sql$")
MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum_sha256 TEXT NOT NULL CHECK(length(checksum_sha256) = 64),
    applied_at TEXT NOT NULL,
    app_version TEXT NOT NULL
) STRICT;
"""


class MigrationError(RuntimeError):
    """A migration set is incompatible with the database's applied history."""


class MigrationChecksumError(MigrationError):
    """An already applied migration no longer has its recorded checksum."""


@dataclass(frozen=True)
class Migration:
    """A numbered SQL migration loaded from package data or a test directory."""

    version: int
    name: str
    sql: str
    checksum_sha256: str

    @property
    def filename(self) -> str:
        return f"{self.version:04d}_{self.name}.sql"


@dataclass(frozen=True)
class AppliedMigration:
    """A migration recorded in a database."""

    version: int
    name: str
    checksum_sha256: str
    applied_at: str
    app_version: str


@dataclass(frozen=True)
class IntegrityReport:
    """Results of SQLite's built-in consistency checks."""

    quick_check: tuple[str, ...]
    integrity_check: tuple[str, ...]
    foreign_key_violations: tuple[tuple[object, ...], ...]

    @property
    def ok(self) -> bool:
        return (
            self.quick_check == ("ok",)
            and self.integrity_check == ("ok",)
            and not self.foreign_key_violations
        )


def connect_database(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with the datasource runtime policy applied.

    Write connections use SQLite's rollback journal, full synchronous commits,
    foreign keys, and a five-second busy timeout.  Read connections use URI
    read-only mode plus ``query_only`` so callers cannot accidentally mutate
    data through a read path.
    """

    database_path = Path(path).expanduser().resolve()
    if read_only:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
    else:
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=5.0, isolation_level=None)

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if read_only:
        connection.execute("PRAGMA query_only = ON")
    else:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


def open_read_connection(path: str | Path) -> sqlite3.Connection:
    """Compatibility-friendly explicit name for a read-only connection."""

    return connect_database(path, read_only=True)


def open_write_connection(path: str | Path) -> sqlite3.Connection:
    """Compatibility-friendly explicit name for a write-policy connection."""

    return connect_database(path)


@contextmanager
def transaction(connection: sqlite3.Connection, *, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
    """Run a bounded SQLite transaction, rolling back on any exception."""

    normalized_mode = mode.upper()
    if normalized_mode not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
        raise ValueError("transaction mode must be DEFERRED, IMMEDIATE, or EXCLUSIVE")
    connection.execute(f"BEGIN {normalized_mode}")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


class MigrationRunner:
    """Apply and validate packaged, immutable, forward-only SQL migrations."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        migration_directory: Path | Traversable | None = None,
        app_version: str = "0.1.0",
    ) -> None:
        self.database_path = Path(database_path)
        self.migration_directory = migration_directory
        self.app_version = app_version

    def discover(self) -> tuple[Migration, ...]:
        """Load migrations and reject malformed or duplicate version files."""

        directory = self.migration_directory
        if directory is None:
            directory = resources.files("nan_fung.storage.migrations")

        migrations: list[Migration] = []
        versions: set[int] = set()
        names: set[str] = set()
        for entry in directory.iterdir():
            if not entry.name.endswith(".sql"):
                continue
            match = MIGRATION_FILENAME.fullmatch(entry.name)
            if match is None:
                raise MigrationError(f"invalid migration filename: {entry.name}")
            version = int(match.group("version"))
            name = match.group("name")
            if version in versions:
                raise MigrationError(f"duplicate migration version: {version}")
            if name in names:
                raise MigrationError(f"duplicate migration name: {name}")
            raw_sql = entry.read_bytes()
            sql = raw_sql.decode("utf-8")
            migrations.append(
                Migration(
                    version=version,
                    name=name,
                    sql=sql,
                    checksum_sha256=sha256(raw_sql).hexdigest(),
                )
            )
            versions.add(version)
            names.add(name)
        if not migrations:
            raise MigrationError("no packaged migrations found")
        return tuple(sorted(migrations, key=lambda migration: migration.version))

    def applied(self, connection: sqlite3.Connection) -> tuple[AppliedMigration, ...]:
        """Return the immutable migration ledger from an initialized database."""

        if not _table_exists(connection, "schema_migration"):
            return ()
        rows = connection.execute(
            "SELECT version, name, checksum_sha256, applied_at, app_version "
            "FROM schema_migration ORDER BY version"
        ).fetchall()
        return tuple(
            AppliedMigration(
                version=row["version"],
                name=row["name"],
                checksum_sha256=row["checksum_sha256"],
                applied_at=row["applied_at"],
                app_version=row["app_version"],
            )
            for row in rows
        )

    def migrate(self) -> tuple[Migration, ...]:
        """Apply outstanding migrations in order and return those applied now."""

        connection = connect_database(self.database_path)
        try:
            return self.apply(connection)
        finally:
            connection.close()

    def apply(self, connection: sqlite3.Connection) -> tuple[Migration, ...]:
        """Apply pending migrations using the supplied write connection."""

        _ensure_migration_table(connection)
        migrations = self.discover()
        applied = self.applied(connection)
        _validate_applied_history(migrations, applied)

        highest_applied = applied[-1].version if applied else 0
        applied_versions = {migration.version for migration in applied}
        pending = tuple(migration for migration in migrations if migration.version not in applied_versions)
        for migration in pending:
            if migration.version < highest_applied:
                raise MigrationError(
                    f"migration {migration.filename} would be applied behind "
                    f"already applied version {highest_applied}"
                )
            self._apply_one(connection, migration)
        return pending

    def validate(self) -> tuple[Migration, ...]:
        """Verify migration checksums and return migrations not yet applied.

        Unlike :meth:`migrate`, this method never creates the migration ledger
        or changes the database.  A daemon can use it at startup to reject an
        unsupported schema rather than applying changes implicitly.
        """

        connection = connect_database(self.database_path, read_only=True)
        try:
            migrations = self.discover()
            applied = self.applied(connection)
            _validate_applied_history(migrations, applied)
            applied_versions = {migration.version for migration in applied}
            return tuple(migration for migration in migrations if migration.version not in applied_versions)
        finally:
            connection.close()

    def _apply_one(self, connection: sqlite3.Connection, migration: Migration) -> None:
        applied_at = _utc_timestamp()
        ledger_insert = (
            "INSERT INTO schema_migration "
            "(version, name, checksum_sha256, applied_at, app_version) VALUES "
            f"({migration.version}, {_sql_literal(migration.name)}, "
            f"{_sql_literal(migration.checksum_sha256)}, {_sql_literal(applied_at)}, "
            f"{_sql_literal(self.app_version)});"
        )
        script = f"BEGIN IMMEDIATE;\n{migration.sql}\n{ledger_insert}\nCOMMIT;"
        try:
            connection.executescript(script)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def integrity_check(path: str | Path) -> IntegrityReport:
    """Run SQLite quick, full, and foreign-key checks without mutating data."""

    connection = connect_database(path, read_only=True)
    try:
        quick_check = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check"))
        full_check = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
        foreign_keys = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
        return IntegrityReport(quick_check, full_check, foreign_keys)
    finally:
        connection.close()


def backup_database(
    source: str | Path,
    target: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Create an atomically published SQLite online backup.

    This intentionally backs up only the database file.  A production backup
    set must additionally copy and verify CAS objects with a manifest.
    """

    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    if target_path.exists() and not overwrite:
        raise FileExistsError(f"backup target already exists: {target_path}")
    target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")

    source_connection = connect_database(source_path, read_only=True)
    target_connection = sqlite3.connect(temporary_path, isolation_level=None)
    try:
        source_connection.backup(target_connection)
        target_connection.close()
        _fsync_file(temporary_path)
        os.replace(temporary_path, target_path)
        _fsync_directory(target_path.parent)
        return target_path
    except BaseException:
        target_connection.close()
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        source_connection.close()


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(MIGRATION_TABLE_SQL)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
        ).fetchone()
        is not None
    )


def _validate_applied_history(
    migrations: tuple[Migration, ...], applied: tuple[AppliedMigration, ...]
) -> None:
    migration_by_version = {migration.version: migration for migration in migrations}
    for applied_migration in applied:
        packaged = migration_by_version.get(applied_migration.version)
        if packaged is None:
            raise MigrationError(
                f"applied migration {applied_migration.version} is not packaged by this version"
            )
        if packaged.name != applied_migration.name:
            raise MigrationError(
                f"migration {applied_migration.version} was renamed from "
                f"{applied_migration.name} to {packaged.name}"
            )
        if packaged.checksum_sha256 != applied_migration.checksum_sha256:
            raise MigrationChecksumError(
                f"migration {packaged.filename} checksum differs from applied history"
            )


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
