# First use and data pipeline

This guide separates the three boundaries that must not be blurred:

1. **macOS data host** — the only place that may run scheduled ingestion,
   because parser isolation depends on `sandbox-exec`.
2. **canonical store** — SQLite plus immutable evidence, owned by one writer.
3. **dashboard/runtime** — reads canonical data through the typed Facade; it
   never gains SQL, filesystem, shell, collector, or raw-evidence tools.

## 1. Configure a secure data host

Create a private TOML configuration (mode `0600`) and a private cursor-secret
file. Production must declare `data_dir`, `backup_dir`, `cursor_secret_file`,
and `operator_role`; an example is in
[datasource operations](datasource-operations.md#install-and-configure-a-clean-host).

Before enabling any network work, validate both the database and the platform
boundary:

```sh
cre --config /secure/cre.toml db migrate
cre --config /secure/cre.toml health
cre --config /secure/cre.toml datasource status
```

Do not enable the daemon if `parser_isolation.available` or
`operational_ingestion.ready` is false. A Linux Docker host can serve reads but
is not an approved ingestion host.

## 2. Materialize and run bounded work

Every operator command emits one JSON document. Read `ok`, the exit code, and
the reported job state rather than parsing stderr text.

```sh
cre --config /secure/cre.toml ingest enqueue boe.bank_rate.iudbedr \
  --request '{"series":"IUDBEDR"}'
cre --config /secure/cre.toml scheduler tick
cre --config /secure/cre.toml daemon once --allow-network
cre --config /secure/cre.toml observations latest --kind metrics --limit 20
```

Use `daemon once` from a trusted host scheduler, or `daemon run
--allow-network --poll-interval-seconds 30` for a resident host service. A
completed refresh/job is not itself a market update: query canonical data again
and propagate its `as_of`, freshness, degraded state, and citations.

The only agent dashboard metric today is `uk.bank-rate-current`. Other bound
engineering workflows do not automatically become dashboard/product coverage;
consult [datasource acceptance](datasource-acceptance.md) before claiming that
a product case is answerable.

## 3. Configure the runtime

`agent-runtime/.env` is for local Node execution and is intentionally separate
from the root Compose `.env`:

```dotenv
PI_MODEL=glm/GLM-5.2
PI_BASE_URL=https://your-provider.example/v1
PI_API_KEY=replace-me
CRE_DATA_DIR=/absolute/path/to/canonical-data
```

Run `npm run start` within `agent-runtime/`. The process refuses to start
without all PI values and a canonical data directory. The browser receives an
ephemeral bearer only in memory; it uses authenticated `fetch` for SSE because
native `EventSource` cannot attach that header.

## 4. Serve it with Docker

The Docker image is runtime/read-only. It migrates an empty `/data` volume then
starts the Node service; it neither runs the scheduler nor collects data.

```sh
cp .env.example .env
chmod 600 .env
docker compose up --build
```

Keep `CRE_DATA_VOLUME=cre-data` for a fresh demo volume. To attach host-managed
canonical data, point it at an absolute shared macOS path:

```dotenv
CRE_DATA_VOLUME=/absolute/path/to/canonical-data
```

The path must be shared with Docker Desktop or Colima. In particular, Colima
may map macOS `/tmp` to a separate empty VM directory; prefer a deliberately
shared directory under the user home or project volume and verify with a
read-only `observations latest` call after startup.

Do not mount a directory used by another active writer unless the host's
single-writer policy has been considered. The dashboard/Facade child performs
read-only queries; ingestion remains the host's responsibility.

## 5. First checks and troubleshooting

- A blank/new store is expected to show dashboard coverage as unavailable.
- Run `cre ... health` on the data host before investigating the dashboard.
- Run `cd agent-runtime && npm test && npm run typecheck` for runtime checks.
- Run `uv run pytest`; currently one unrelated submarket mapping regression is
  known at the repository baseline, so record it separately from dashboard work.
- Never commit `.env`, canonical `data/`, raw evidence, backup files, or API
  keys. `.gitignore` and `.dockerignore` exclude them.
