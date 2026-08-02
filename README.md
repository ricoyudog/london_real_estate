# London Market Desk

London Market Desk is an evidence-first London office research PoC. It combines
a Python canonical data plane (SQLite + immutable evidence), a controlled Pi
agent runtime, and one same-origin dashboard.

The current launch scope is deliberately narrow: **UK Bank Rate is the only
supported numeric market signal.** London office rent, vacancy, transactions,
and ranked market news are visibly unavailable until approved canonical sources
are delivered. The system must never fill those gaps with model-generated
numbers.

![GLM-5.2 host-finalized Bank Rate brief](wiki/questions/Test_result/screenshots/dashboard-glm-desktop.jpg)

The `5.25 percent` shown here is a deterministic demo fixture, not a live market
claim. See the recorded [browser test](wiki/questions/Test_result/TC-01-dashboard-ui-test-2026-08-02.md).

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

## One-command Docker demo

Create the untracked credentials file once, set `PI_BASE_URL` and `PI_API_KEY`,
then start the complete demo:

```sh
cp agent-runtime/.env.example agent-runtime/.env
chmod 600 agent-runtime/.env
docker compose up --build --wait
```

Open <http://127.0.0.1:8787>. Compose first runs the one-shot
`demo-data-init` service, migrates the dedicated `market-desk-demo-data` volume,
verifies the packaged Bank Rate fixture checksum, and writes a versioned marker.
The UI, HTTP/SSE transport, typed Facade, and Pi `createAgentSession` runtime
then start behind a healthcheck suitable for `--wait`.

This mode is explicitly a reproducible fixture demo. It does not run live
ingestion or grant a refresh profile. Linux containers must not run `cre daemon`
because parser isolation depends on macOS `sandbox-exec`.

```sh
docker compose down      # stop containers; preserve the seeded demo volume
docker compose up --wait # restart and verify the existing marker/data

docker compose down -v   # remove only this Compose project's demo volume
docker compose up --build --wait # migrate and seed it again
```

The initializer fails closed outside `MARKET_DESK_MODE=demo`, including when a
demo marker is found in a non-demo startup.

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
cd agent-runtime
npm test
npm run typecheck
npm run test:browser
npm audit --omit=dev
```

With a configured private `.env`, `npm run test:glm` performs the opt-in real
GLM-5.2/Pi acceptance without a fake session factory or model override. The
dated deterministic, browser, Docker, and live-model results are recorded in
[`tests/Test case.md`](tests/Test%20case.md).

## Local state and secrets

`.env`, local data, virtual environments, node modules, evidence scratch space,
generated browser reports, and editor state are ignored. Never commit the
credentials file, API keys, canonical data, raw evidence, or backups.
