## OVERVIEW

Package coordination layer: operator entry points, daemon dispatch, configuration, durable mutations, backup orchestration, and Bank Rate workflow adapters. Domain implementation lives in the sibling subpackages below.

## ROOT MODULES

| File | Role | Key symbols |
| --- | --- | --- |
| `cli.py` | `cre` argument parsing, authorization, JSON result envelope, command dispatch. | `main`, `_build_parser`, `_dispatch`, `_authorize_command` |
| `supervisor.py` | Resident scheduler and worker: claim jobs, select datasource lifecycle, report ticks. | `DatasourceSupervisor`, `SupervisorTick`, `SupervisorRun` |
| `operational.py` | Durable mutation boundary over the operational database and evidence store. | `OperationalStore`, `OperationalError`, `RunHandle` |
| `config.py` | TOML, environment, and CLI override loading; runtime path and secret validation. | `AppConfig`, `load_config`, `load_cursor_secret` |
| `backups.py` | Verified database plus referenced-evidence backup creation, verification, and restore. | `create_backup_set`, `verify_backup_set`, `restore_backup_set` |
| `workflows.py` | Bank Rate acquire, ingest, and offline reparse adapters over ingestion and storage. | `acquire_live_bank_rate`, `ingest_bank_rate_artifact`, `reparse_bank_rate_evidence` |

`__init__.py` is package metadata only; no coordination surface.

## SUBPACKAGE MAP

| Subpackage | What lives here, when to edit it |
| --- | --- |
| `agent_tools/` | Trusted subprocess facade and JSON contracts; edit for agent callable tools. |
| `datasources/` | Source-specific acquisition adapters, host controls, and legacy result adapters; edit for source fetch behavior. |
| `ingestion/` | Registry, job types, canonical records, lifecycle workflows, and parser sandbox bindings; edit for ingestion semantics. |
| `projections/` | Rebuildable read-model rows and delivered artifacts; edit for projection generation or publication. |
| `read_api/` | Typed query contracts, access context, cursoring, and SQLite reads; edit for canonical read behavior. |
| `refresh_api/` | Bounded refresh broker in `broker.py`, request/status contracts in `contracts.py`, durable adapter in `operational_backend.py`; edit for refresh approval and enqueue flow. |
| `storage/` | Database migrations, connections, and evidence storage; edit for persistence primitives. |

## WHERE TO LOOK

| Task | Files |
| --- | --- |
| Add an operator command | `cli.py`, then the owning subpackage or top-level adapter |
| Add a daemon-dispatched workflow | `supervisor.py`, `workflows.py` or its ingestion lifecycle |
| Change CLI or daemon runtime configuration | `config.py`, callers in `cli.py` or `supervisor.py` |
| Add backup, verification, or restore behavior | `backups.py`, `storage/` |
| Change Bank Rate ingest or replay behavior | `workflows.py`, `ingestion/bank_rate.py` |
| Change durable refresh request handling | `refresh_api/`, `operational.py` only for existing persistence hooks |

## ANTI-PATTERNS (THIS DIRECTORY)

- Do not bypass `OperationalStore` for SQLite writes.
- Do not extend `operational.py` inline; compose a focused module around its existing boundary.
- Do not call evidence parsers directly; route through the `ingestion/parser_runner.py` sandbox.
