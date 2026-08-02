## OVERVIEW

Rebuildable, deterministic projections over canonical records. Delivery publishes atomically; wiki and alerts render derived outputs.

## STRUCTURE

| File | Role |
| --- | --- |
| `models.py` | Projection contracts, canonical gate, row builders, access class. |
| `rebuild.py` | Deterministic rebuild of derived SQLite indexes. |
| `delivery.py` | Fixed artifacts, source/content hashes, atomic filesystem publish. |
| `snapshots.py` | Deterministic daily and weekly snapshot input and IDs. |
| `wiki.py` | Canonical projection rows to wiki page rendering. |
| `alerts.py` | Deterministic threshold alert evaluation. |
| `__init__.py` | Public projection exports. |

## PROJECTION KINDS

| Kind | `query_kind` match | Example datasource |
| --- | --- | --- |
| `metrics` | `metrics` | `boe.bank_rate.iudbedr` |
| `supply` | `supply` | Supply datasource with `record_type="supply"` |
| `events` | `events` | Event datasource with `record_type="event"` |
| `geographies` | `geographies` | `ons.onspd.postcode` |

`build_projection_rows()` requires exact `record.query_kind == projection_kind`.

## DELIVERY CONTRACT

Atomic publish path: deterministic snapshot source hash -> artifact JSON via `_json_bytes()` -> content SHA-256 -> hard link.

`_json_bytes()` uses sorted keys, compact separators, `ensure_ascii=False`, `allow_nan=False`, and a trailing newline.
`_snapshot_source_hash()` requires a deterministic `snap_<sha256>` ID before delivery.

## WHERE TO LOOK

| Task | Location |
| --- | --- |
| Add a projection kind | `models.py`: `PROJECTION_KINDS`, builders, then delivery query kinds. |
| Change delivery artifact format | `delivery.py`: `_render_delivery()`, serializers, `_json_bytes()`. |
| Change snapshot determinism | `snapshots.py`: `build_snapshot()` and semantic-row hash input. |
| Change wiki rendering | `wiki.py`: `render_market_wiki()`. |
| Add an alert rule | `alerts.py`; pass normalized rules through `delivery.py`. |

## ANTI-PATTERNS (THIS DIRECTORY)

- Do not build projections from non-canonical records. `_require_canonical()` rejects non-canonical and non-`production_ingestion` inputs.
- Do not project a record whose `query_kind` mismatches `projection_kind`.
- Do not use default `json.dumps`. Use `_json_bytes()` with sorted keys, compact separators, `ensure_ascii=False`, `allow_nan=False`, and a trailing newline.
- Do not mutate `ProjectionRow` or `DeliveredProjectionArtifact` fields. Both are frozen.
- Do not skip the source-hash determinism check.
