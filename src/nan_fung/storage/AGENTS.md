## OVERVIEW

`storage/` provides the durable SQLite schema boundary and immutable CAS artifact store.
It packages numbered, forward-only SQL migrations with checksum-verified history.

## STRUCTURE

| File | Role |
| --- | --- |
| `__init__.py` | Public storage primitives: CAS, migrations, connections, checks, backups. |
| `db.py` | SQLite connection safety, migration ledger, validation, integrity checks, atomic database backups. |
| `artifacts.py` | Immutable SHA-256 CAS publication, verification, reads, and digest enumeration. |
| `migrations/__init__.py` | Packaged forward-only migration namespace. |
| `migrations/*.sql` | Ordered SQLite schema evolution, shipped in the wheel. |

## MIGRATIONS

| Migration | Purpose |
| --- | --- |
| `0001_schema_migration.sql` | Creates the immutable migration ledger. |
| `0002_operational_tables.sql` | Adds operational provenance tables and derived projection indexes. |
| `0003_canonical_views.sql` | Adds workflow indexes and current canonical views. |
| `0004_manual_promotion.sql` | Records immutable links from approved reviews to promotions. |
| `0005_append_only_guards.sql` | Adds triggers preventing in-place changes to immutable provenance. |
| `0006_refresh_request_ledger.sql` | Persists append-only refresh idempotency and job status ledger. |
| `0007_refresh_confirmation.sql` | Adds append-only second-confirmation evidence for budgeted refreshes. |
| `0008_agent_tool_approval.sql` | Adds host-only durable approval capability and event records. |

## CAS ARTIFACT LIFECYCLE

1. Create a private temp file in `evidence/.tmp` with `O_NOFOLLOW`.
2. Stream bytes while hashing with SHA-256.
3. Flush and `fsync` the temp file.
4. Run the optional validator against that private, fsynced file.
5. Hard-link it to `evidence/sha256/<prefix>/<hash>`.
6. Remove the temp file and `fsync` the destination directory.
7. If the object already exists, verify it with `lstat`, size check, and rehash, then reuse it.

Existing CAS objects are never replaced.

## WHERE TO LOOK

| Task | Location |
| --- | --- |
| Add a migration | `migrations/000N_name.sql`; preserve numeric order and package discovery. |
| Change CAS publication | `artifacts.py:ArtifactStore.put_stream`. |
| Add an integrity check | `db.py:integrity_check`. |
| Change applied-history validation | `db.py:_validate_applied_history`. |

## ANTI-PATTERNS (THIS DIRECTORY)

- Do not rename or reorder migrations. Applied checksums and names are durable history.
- Do not replace existing CAS objects. Verify and reuse them only.
- Do not follow symlinks for temp files. Keep `O_NOFOLLOW` in `_open_temporary`.
- Do not open SQLite outside `connect_database`.
- Do not write canonical tables directly. Use `OperationalStore`, which composes through this package.
- Do not skip `_validate_applied_history` before migration application or validation.
