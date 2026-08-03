# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-03
**Commit:** 16c0052
**Branch:** main

## OVERVIEW

`nan-fung` is a stateful Python data plane for London office market research: it ingests free public datasources through policy-bounded HTTP, persists evidence in content-addressed storage with SQLite canonical records, and exposes typed read/refresh APIs plus a versioned agent-tool subprocess facade. Two CLIs ship from one `src/nan_fung` package.

## STRUCTURE

```
nan_fung/
├── src/nan_fung/         # one package, multiple bounded planes
│   ├── cli.py            # `cre` operator CLI (every command emits one JSON doc)
│   ├── supervisor.py     # `cre daemon` resident worker (single-writer lease)
│   ├── operational.py    # OperationalStore: durable SQLite mutation boundary (~4900 LOC)
│   ├── config.py         # CRE_* env + TOML config; production validation
│   ├── backups.py        # backup/verify/restore over the writer lease
│   ├── workflows.py      # bank_rate ingestion workflow
│   ├── ingestion/        # canonical JSON, policies, registry, jobs, parser sandbox, lifecycles
│   ├── datasources/      # domain fetchers (legacy SourceResult adapters)
│   ├── storage/          # SQLite + CAS artifact store + 8 numbered migrations
│   ├── projections/      # rebuildable projections + atomic delivery + wiki rendering
│   ├── read_api/         # typed in-process read service, keyset pagination, citation projection
│   ├── refresh_api/      # bounded refresh broker + operational backend
│   └── agent_tools/      # `nan-fung-agent-tools` subprocess facade + JSON contracts
├── tests/                # 49 modules, 402 collected; offline-by-default pytest
├── agent-runtime/        # Pi/TypeScript runtime + same-origin HTTP/SSE dashboard
├── demo/                 # deterministic Docker fixture initializer and asset
├── docker-compose.yml    # one-shot init + health-gated market-desk service
├── skills/               # 7 OpenAI-agent skill bundles (SKILL.md + agents/openai.yaml)
├── wiki/                 # tracked Obsidian vault: architecture, decisions, research
├── docs/                 # datasource-operations + datasource-acceptance runbooks
├── memory/               # session-bridge + pitfalls (agent process state, tracked)
├── openspec/             # openspec/config.yaml — tracks GitHub ricoyudog/london_real_estate
├── pyproject.toml        # hatchling; deps: odfpy, openpyxl, pypdf; dev: jsonschema, pytest
└── uv.lock
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add/modify an operator command | `src/nan_fung/cli.py` (`_build_parser`, `_dispatch`, `_authorize_command`) | Every command path emits exactly one JSON document on stdout. Errors never reach stdout as text. |
| Change ingestion workflow | `src/nan_fung/ingestion/<domain>_workflow.py` + `supervisor.py` | Daemon dispatches by `datasource_id` to a workflow; new datasources need a workflow binding. |
| Add a new SQL migration | `src/nan_fung/storage/migrations/00NN_name.sql` | Numbered, checksum-verified, append-only. Wheel packages `*.sql`. |
| Add a new agent tool | `src/nan_fung/agent_tools/` (facade + JSON contracts) | Must update both `agent_tool_contracts.v1.json` and Python facade. Schema is the contract. |
| Add a datasource fetcher | `src/nan_fung/datasources/<domain>.py` | Legacy `SourceResult` adapter pattern; canonical ingestion goes through workflows. |
| Change read API filtering | `src/nan_fung/read_api/contracts.py` (`ALLOWED_FILTERS`, `QUERY_KINDS`) + `sqlite_repository.py` | Keyset-paginated, as-of, access-class scoped. |
| Modify provenance/evidence | `src/nan_fung/storage/artifacts.py` + `ingestion/canonical.py` | CAS store: `evidence/sha256/<prefix>/<hash>`. Hashing is domain-separated. |
| Refresh request flow | `src/nan_fung/refresh_api/` + `operational.py` (refresh ledger) | Cooldowns, fingerprints, confirmation tokens, approval gate. |

## CODE MAP

Highest-centrality symbols (edit with blast radius in mind):

| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `OperationalStore` | class | `operational.py:271` | 134 | Single-writer mutation boundary over SQLite + CAS. Almost every stateful path routes here. |
| `DatasourceSupervisor` | class | `supervisor.py:64` | 26 | Resident daemon: registry sync → heartbeat → scheduler tick → claim one job → workflow. |
| `ReadService` | class | `read_api/service.py:54` | 20 | Typed read API entry; cursor HMAC, as-of, access class. |
| `ReadQuery` / `ReadContext` | dataclass | `read_api/contracts.py:107` / `access.py:50` | 37 / 38 | Read request shape + principal/access envelope. |
| `ArtifactStore` | class | `storage/artifacts.py:52` | 33 | Immutable CAS publish via temp file → fsync → hard link. |
| `MigrationRunner` | class | `storage/db.py:147` | 17 | Numbered migration application + checksum verification. |
| `AgentToolHost` | class | `agent_tools/host.py:52` | 14 | Trusted-side subprocess launcher; fixed argv, FD 3 secret, timeout, output bounds. |
| `CanonicalizationError` | class | `ingestion/canonical.py:31` | 19 | Domain-separated canonical JSON hashing for records, evidence, watermarks. |
| `SourceResult` | TypedDict | `datasources/common.py:234` | 7 | Legacy adapter contract; not canonical. |
| `build_projection_rows` | function | `projections/models.py:87` | — | Deterministic canonical-record → projection mapper. |

## CONVENTIONS

- **One JSON document per CLI invocation.** `cre` always emits exactly one JSON object on stdout (`schema_version: cre_cli.v1`), success or failure. Diagnostics go to stderr only. Exit `0` ok, `2` expected error, `1` unexpected.
- **src layout, `pythonpath = ["src"]`.** Tests import `nan_fung.*` directly; wheel built from `src/nan_fung`.
- **Pytest offline by default.** `addopts = "-m 'not live and not network'"`. Markers: `network`, `live`, `legacy_live_probe`, `restricted_live_probe`. Opt in explicitly with `-m live`.
- **Wheel packages non-Python assets.** `storage/migrations/*.sql` and `agent_tools/*.json` are explicitly included by Hatch.
- **Production config is private and required.** `production` env requires explicit `data_dir`, `backup_dir`, `cursor_secret_file`, `operator_role`. Config + secret files must be `0600`; cursor secret 32–4096 bytes, read with `O_NOFOLLOW`.
- **Single-writer lease.** `OperationalStore` methods are decorated `@_single_writer`. Daemon is one process; no concurrent writers.
- **Canonical JSON is deterministic.** `canonical_json` / `hash_canonical` use domain-separated SHA-256 prefixes (`nan-fung/<domain>/v1\0`). Duplicate JSON keys are rejected at parse time.
- **Evidence is immutable and content-addressed.** CAS publish goes temp → fsync → hard link; existing objects are never replaced, only verified.
- **Timestamps are tz-aware UTC.** Naive datetimes raise; everything normalizes through `_normalise_utc`.
- **Access classes are layered.** `most_restrictive_access` combines datasource/evidence/source access classes; read API filters by principal's allowed set.
- **Lane promotion gates canonical reads.** Projections and the read API only consume `production_ingestion` lane records; `source_discovery` and `ad_hoc_research` lanes are not canonical.

## ANTI-PATTERNS (THIS PROJECT)

- **Do not add a `conftest.py` or shared fixture factory.** Setup is intentionally local per test file (`tmp_path`, `monkeypatch`, per-file `_store`/`_seed_store`/`_fixture` helpers). Only the agent-tool protocol uses shared JSON fixtures (`tests/fixtures/agent_tools/v1/`).
- **Do not introduce Ruff/Black/mypy/isort config without explicit ask.** Project ships no formatter/linter config; do not silently add one.
- **Do not parse evidence in-process without the sandbox.** Parsers run under macOS `sandbox-exec` via `ingestion/parser_runner.py`. Adding a parser means binding it through the sandbox path, not calling it directly.
- **Do not extend `OperationalStore` casually.** It is already ~4900 LOC. New stateful surfaces go in their own module and compose through it.
- **Do not add a second HTTP server.** The same-origin Node dashboard/runtime owns HTTP/SSE; Python reads stay in-process and agent tools remain a subprocess contract.
- **Do not follow symlinks for operator-supplied files.** `_read_fixture`, cursor secret, and CAS temp files all open with `O_NOFOLLOW`.
- **Do not emit parser tracebacks across the sandbox boundary.** `_sandbox_child_main` strips tracebacks to `PARSER_<ErrorType>` codes only.
- **Do not rename or reorder existing migrations.** Checksums are recorded at apply time; `_validate_applied_history` will refuse to start.

## UNIQUE STYLES

- **Two CLIs, intentionally separate parsers.** `nan-fung-agent-tools` deliberately does not reuse `cre`'s argparse — its contract is `<tool-name>` + one JSON on stdin/stdout.
- **Frozen dataclasses with `__post_init__` validators.** Domain types (`ProjectionRow`, `BankRateRecord`, `CitationProjection`, `DeliveredProjectionArtifact`) normalize and validate in `__post_init__` via `object.__setattr__`.
- **`MappingProxyType` for frozen mappings.** `freeze_json` deep-freezes canonical descriptors; mutable copies via `thaw_json`.
- **Domain-separated hashing prefixes.** `nan-fung/<domain>/v1\0`, `nan-fung/record-key/v1\0`, etc. — never hash raw JSON without a domain prefix.
- **Agent-tool handle secret on FD 3.** Inherited file descriptor, not argv/env. Loaded by `load_handle_secret_from_fd()`.
- **Bank Rate values are decimal strings.** `_decimal_text` rejects exponent notation and `-0`; `Decimal`-validated.
- **Session memory protocol (CLAUDE.md).** Startup reads `memory/session-bridge.md` → `wiki/hot.md` → `wiki/index.md` (max 3). Wiki has hard size caps. Not standard for Python repos; treat `memory/` and `wiki/` as tracked agent process state.

## COMMANDS

```bash
# Install (dev)
uv sync

# Build wheel + sdist
uv build

# Run offline test gate (default; excludes live/network)
uv run pytest

# Run live smoke tests (requires approved network access)
uv run pytest -m live

# Run one CLI command (development data dir)
uv run cre --data-dir ./data db migrate
uv run cre --data-dir ./data health
uv run cre --data-dir ./data registry diff
uv run cre --data-dir ./data datasource status

# Daemon (production-shaped; macOS host for sandbox-exec)
uv run cre --config /etc/cre/cre.toml daemon once --allow-network
uv run cre --config /etc/cre/cre.toml daemon run --allow-network --poll-interval-seconds 30

# Agent-tool subprocess (driven by trusted host, not humans)
nan-fung-agent-tools <tool-name> < request.json > result.json
```

## NOTES

- **Naming drift.** Package is `nan-fung`/`nan_fung`; OpenSpec tracks GitHub `ricoyudog/london_real_estate`. Likely historical.
- **macOS dependency for ingestion.** Parser isolation uses `sandbox-exec`. Non-macOS hosts can run reads/migrations/health but ingestion should stay disabled unless `health` reports `parser_isolation.available`.
- **No CI.** Production ingestion still needs an external macOS service manager; the Docker stack is a deterministic read/runtime demo and never runs the daemon.
- **Node/dashboard runtime is present.** `agent-runtime/` implements Pi `createAgentSession`, HTTP/SSE, host finalization, and the same-origin UI.
- **Offline gate is green.** Current verification is `387 passed, 15 deselected`; update this note when the collected suite changes.
- **`operational.py` is a known hotspot.** ~4900 LOC, 134 callers. Edit carefully; prefer composing through it from a new module.
- **`.codegraph` is a gitignored symlink** to local CodeGraph state — first `git add -A` staged it accidentally; see `memory/pitfalls`.
