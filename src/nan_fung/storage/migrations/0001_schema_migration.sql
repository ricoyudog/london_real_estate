CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum_sha256 TEXT NOT NULL CHECK(length(checksum_sha256) = 64),
    applied_at TEXT NOT NULL,
    app_version TEXT NOT NULL
) STRICT;
