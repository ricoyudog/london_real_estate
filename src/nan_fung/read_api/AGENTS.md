## OVERVIEW

Typed in-process read service for canonical records. Keyset-paginated, as-of,
access-class scoped, with safe citation projections.

## STRUCTURE

| File | Role |
| --- | --- |
| `__init__.py` | Public read API exports. |
| `access.py` | Access classes, `ReadContext`, UTC and access helpers. |
| `contracts.py` | Bounded query and response contracts. |
| `service.py` | `ReadService`, HMAC cursor handling, pagination orchestration. |
| `sqlite_repository.py` | Canonical as-of selection, keyset SQL, SQLite read repository. |
| `citation.py` | Safe citation metadata and exact-lineage projection. |

## REQUEST SHAPE

| `ReadQuery` field | Type | Constraint |
| --- | --- | --- |
| `query_kind` | `str` | Must be in `QUERY_KINDS`. |
| `filters` | `Mapping[str, str | tuple[str, ...]]` | Keys must be in `ALLOWED_FILTERS`; values are bounded non-empty strings. |
| `as_of` | `datetime | None` | Timezone-aware; normalized to UTC. |
| `cursor` | `str | None` | Bounded, HMAC-signed, validated against request and context. |
| `limit` | `int` | Capped by the contract, currently 1 through 100. |

## ACCESS MODEL

`ReadContext` carries the principal and its `allowed_access_classes`.
`most_restrictive_access` combines datasource, evidence, and source access classes.
Every query filters records to the principal's allowed set.

## CITATION PROJECTION

`anchor_as_of` + `canonical_run_id` + `observation_id` + `evidence_id` +
`locator_hash` identifies a `CitationProjection`.

Projections expose safe metadata only, never a raw-evidence retrieval route.
Missing or unreadable lineage yields no projection, never a caller-visible error.

## WHERE TO LOOK

| Task | Location |
| --- | --- |
| Add a query kind | `contracts.py`: `QUERY_KINDS`; repository query mapping. |
| Add a filter | `contracts.py`: `ALLOWED_FILTERS`; `sqlite_repository.py` SQL filtering. |
| Change pagination cursor | `service.py`: cursor encode, decode, validation, keyset state. |
| Change citation projection shape | `citation.py`: `CitationProjection`; repository projection SQL. |
| Change an access class | `access.py`: `AccessClass`, `ReadContext`, access helpers. |

## ANTI-PATTERNS (THIS DIRECTORY)

- Do not accept filter keys outside `ALLOWED_FILTERS`.
- Do not accept query kinds outside `QUERY_KINDS`.
- Do not return raw evidence paths, only `CitationProjection` metadata.
- Do not skip cursor HMAC verification.
- Do not treat a missing citation as an error. Absence is the safe answer.
- Do not use naive datetimes in `ReadQuery` or `ReadContext`. Use timezone-aware UTC.
