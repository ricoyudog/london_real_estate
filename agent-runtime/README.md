# Pi Agent Runtime

## Overview

Pi Agent Runtime is a controlled Node/TypeScript agent service for UK Bank Rate
and borough/month planning-application activity. The model reaches market data
only through six typed tools backed by the `nan-fung-agent-tools` facade. The
host owns budgets, citations, and approval. The runtime produces a versioned
`market_brief.v1` result rather than direct model output.

## Requirements

- Node `>= 22.19.0`.
- The Python data plane and `nan-fung-agent-tools` installed in the worktree virtual environment.
- A seeded canonical Bank Rate store.

## Install and Typecheck

From `agent-runtime/`, install the lockfile-pinned dependencies:

```sh
npm ci
```

The lockfile is authoritative. Typecheck with:

```sh
npx tsc --noEmit
```

## Environment and Run

Create `agent-runtime/.env` from `.env.example` and keep it owner-readable only:

```sh
cp .env.example .env
chmod 600 .env
```

- `PI_MODEL=glm/GLM-5.2`, `PI_BASE_URL`, and `PI_API_KEY` configure the fixed production GLM provider. The variable names remain generic even when the endpoint is hosted by another provider.
- `CRE_DATA_DIR=<absolute path to the seeded store>` is required because the child facade launcher uses it.
- A deployment launcher must pass the file with Node's `--env-file=.env`; the `test:glm` command below already does so.

Start the same-origin dashboard and runtime service:

```sh
npm run start
```

Open <http://127.0.0.1:8787>. The dashboard reads its Bank Rate overview from
the typed Facade and uses authenticated `fetch` SSE for chat turns; browser
native `EventSource` cannot attach the session bearer. See the root
[first-use guide](../docs/first-use-and-data-pipeline.md) for the data-host and
Docker boundaries.

## Test

Run the top-level suites:

```sh
npm test
```

Run the seven deterministic `fauxProvider` fixtures through the real `createAgentSession` path:

```sh
node --experimental-strip-types --test test/fixtures/*.test.ts
```

Run the opt-in live GLM gate with the local `.env`:

```sh
npm run test:glm
```

The fixtures cover ambiguous dates, explicit historical values, fresh latest values, successful refresh, failed refresh, absent canonical coverage, and blocked TC-01 coverage. Generated, gitignored manifests live under `test/.evidence/fixtures/` and `test/.evidence/integration-2b/`; the sanitized release result is recorded in `../tests/Test case.md`.

## Production vs Test Policy

The production allowlist exposes nine capabilities: `uk.bank-rate-current`,
`london-planning-activity`, `uk.gdp.current`, `uk.inflation.current`,
`uk.labour.current`, `uk.employment.london`, `uk.hybrid-working`,
`london.office-stock`, and `london.epc-certificates`. Bank Rate and planning
activity retain their `bank-rate-latest` and `planning-activity-monthly`
refresh profiles. The seven new capabilities are query-only. Planning activity
is a borough/month, all-use-class proxy, not office-only supply or floorspace.
The ONSPD postcode capability, `uk.postcode-resolution` with
`onspd-one-postcode`, exists only behind the hidden `test-online-v1` policy
used by the Phase 2c approval gate. It is never in production.

## What this runtime does NOT do

- Answer London office rent, vacancy, news, or transaction questions. Those coverage areas are blocked.
- Provide production auth or tenancy.
- Give the model filesystem, shell, or network tools. It has only the six typed tools.
- Write canonical data. The Python data plane owns persistence.
- Run real-model smoke by default.
- Roll out ONSPD in production.

## Product coverage limit

The dashboard and runtime expose nine capabilities: UK Bank Rate,
borough/month planning activity, UK GDP, UK inflation, UK labour, London
employment, Great Britain hybrid-working survey results, London office stock,
and London EPC lodgements. GDP, inflation, and labour are UK macro indicators;
hybrid working is not office occupancy; office stock is not vacancy or
floorspace; EPC lodgements cover all non-domestic properties. London office
rent, vacancy, news, transaction, and project-supply questions are finalized as
explicit unavailable coverage until their canonical source gates are approved;
the UI does not invent KPI cards for them.

## Scope Claim

a controlled Pi session and dashboard can produce a replayable,
host-hydrated, citation-grounded `market_brief.v1` with explicit coverage
limits, sourced from canonical Bank Rate or planning-application activity.
