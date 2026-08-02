# London Market Desk

London Market Desk is an evidence-first London office research PoC. It combines
a Python canonical data plane (SQLite + immutable evidence), a controlled Pi
agent runtime, and one same-origin dashboard.

The current launch scope is deliberately narrow: **UK Bank Rate is the only
supported numeric market signal.** London office rent, vacancy, transactions,
and ranked market news are visibly unavailable until approved canonical sources
are delivered. The system must never fill those gaps with model-generated
numbers.

![Dashboard overview using a temporary test fixture](wiki/questions/Test_result/screenshots/dashboard-overview-bank-rate.png)

![TC-01 shown honestly as unavailable](wiki/questions/Test_result/screenshots/dashboard-tc01-unavailable.png)

The `5.25 percent` shown in these screenshots is a temporary test fixture, not
a live market claim. See the recorded [browser test](wiki/questions/Test_result/TC-01-dashboard-ui-test-2026-08-02.md).

## Fast start (local macOS development)

Requirements: macOS, Python 3.12 via `uv`, Node 22.19+, and a GLM-compatible
endpoint. Install both runtime layers:

```sh
uv sync
(cd agent-runtime && npm ci)
cp agent-runtime/.env.example agent-runtime/.env
chmod 600 agent-runtime/.env
```

Set these values in `agent-runtime/.env`:

```dotenv
PI_MODEL=glm/GLM-5.2
PI_BASE_URL=https://your-provider.example/v1
PI_API_KEY=replace-me
CRE_DATA_DIR=/absolute/path/to/canonical-data
```

Bootstrap the canonical store once from the repository root, then start the
single dashboard service:

```sh
uv run cre --data-dir /absolute/path/to/canonical-data db migrate
cd agent-runtime
npm run start
```

Open <http://127.0.0.1:8787>. The page creates an in-memory bearer session,
gets the fixed dashboard overview from the typed Facade, and streams only
host-finalized market briefs.

For a safe local demonstration (not production data), seed the supplied Bank
Rate fixture before starting the service:

```sh
uv run python agent-runtime/test/helpers/seed_bank_rate.py /absolute/path/to/canonical-data 5.25
```

## Docker service

Copy the root environment example and start Compose:

```sh
cp .env.example .env
chmod 600 .env
docker compose up --build
```

The service listens on <http://127.0.0.1:8787>, persists `/data`, and runs an
idempotent schema migration at startup. With the default `CRE_DATA_VOLUME`, it
uses the named `cre-data` volume and starts with no observations; the UI will
truthfully show unavailable data.

For a dashboard container to read data collected on a supported macOS host,
set `CRE_DATA_VOLUME` in the root `.env` to that host's absolute canonical-data
directory. Docker Desktop must be allowed to share the directory. **Do not run
`cre daemon` or ingestion inside this Linux container:** parser isolation needs
macOS `sandbox-exec`.

## Data pipeline

The operational data pipeline is host-owned and runs on macOS:

```sh
cre --config /secure/cre.toml db migrate
cre --config /secure/cre.toml health
cre --config /secure/cre.toml ingest enqueue boe.bank_rate.iudbedr --request '{"series":"IUDBEDR"}'
cre --config /secure/cre.toml daemon once --allow-network
```

Only enable the daemon after `health` confirms parser isolation and operational
ingestion are ready. The fuller setup, scheduling, source-policy, backfill,
and review rules are in [the first-use and pipeline guide](docs/first-use-and-data-pipeline.md)
and [the datasource operations runbook](docs/datasource-operations.md).

## Verify

```sh
uv run pytest
cd agent-runtime && npm test && npm run typecheck
```

The full Python suite currently has one pre-existing unrelated failure in
`tests/test_submarket_mapping.py`; the runtime/dashboard targeted tests and
Node gate are documented in the test result record above.

## GitHub readiness

`.env`, local data, virtual environments, node modules, evidence scratch space,
and editor state are ignored. The repository is ready to commit or push after
you review the diff, but this change does not push anything remotely.
