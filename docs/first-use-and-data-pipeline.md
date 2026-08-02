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

`agent-runtime/.env` is the single untracked credentials file used by both
local Node execution and the Docker demo:

```dotenv
PI_MODEL=glm/GLM-5.2
PI_BASE_URL=https://your-provider.example/v1
PI_API_KEY=replace-me
CRE_DATA_DIR=/absolute/path/to/canonical-data
```

Run `npm run start` within `agent-runtime/` for native development. The process
refuses to start without all PI values and a canonical data directory. The
browser receives an ephemeral bearer only in memory; it uses authenticated
`fetch` for SSE because native `EventSource` cannot attach that header.

## 4. Serve it with Docker

The Compose stack is a deterministic fixture demo. The Linux image is still
runtime/read-only: it neither runs the scheduler nor collects live data. A
one-shot `demo-data-init` service migrates the dedicated named volume, verifies
the packaged fixture checksum, seeds canonical Bank Rate data once, and writes
a versioned marker before the Node service starts.

```sh
cp agent-runtime/.env.example agent-runtime/.env
chmod 600 agent-runtime/.env
# Set PI_BASE_URL and PI_API_KEY in agent-runtime/.env.
docker compose up --build --wait
```

`market-desk` starts only after initialization succeeds and exposes a healthcheck
for `docker compose --wait`. Demo sessions intentionally receive no refresh
profile because no ingestion daemon exists in the container.

Use `docker compose down` to stop the stack while retaining fixture data. A
subsequent `docker compose up --wait` verifies marker, database integrity, and
the canonical observation without reseeding. Use `docker compose down -v` only
when the dedicated demo volume should be removed; the next `up --build --wait`
recreates it automatically. A non-demo startup that encounters the demo marker
fails closed.

## 5. First checks and troubleshooting

- The Docker demo must show its fixture banner and canonical `5.25 percent`
  Bank Rate card; it is never a live-rate claim.
- Run `cre ... health` on the data host before investigating the dashboard.
- Run `cd agent-runtime && npm test && npm run typecheck && npm run test:browser`
  for runtime and UI checks.
- Run `uv run pytest` for the complete offline Python gate.
- Run `cd agent-runtime && npm run test:glm` only with the private GLM
  credentials configured; the test forbids a fake session factory and model
  override.
- Never commit `.env`, canonical `data/`, raw evidence, backup files, or API
  keys. `.gitignore` and `.dockerignore` exclude them.
