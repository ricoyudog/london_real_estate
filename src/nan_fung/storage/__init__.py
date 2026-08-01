"""Durable local storage primitives for datasource workflows.

The package deliberately exposes small building blocks.  Higher-level
ingestion code owns evidence metadata, runs, and observation lifecycles;
this package owns SQLite operational safety and immutable content bytes.
"""

from .artifacts import ArtifactStore, StoredArtifact
from .db import (
    IntegrityReport,
    Migration,
    MigrationRunner,
    backup_database,
    connect_database,
    integrity_check,
)

__all__ = [
    "ArtifactStore",
    "IntegrityReport",
    "Migration",
    "MigrationRunner",
    "StoredArtifact",
    "backup_database",
    "connect_database",
    "integrity_check",
]
