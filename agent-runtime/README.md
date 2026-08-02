# Pi Agent Runtime

## Overview

Pi Agent Runtime is a controlled Node/TypeScript agent service for the UK Bank Rate vertical slice. The model reaches market data only through six typed tools backed by the `nan-fung-agent-tools` facade. The host owns budgets, citations, and approval. The runtime produces a versioned `market_brief.v1` result rather than direct model output.

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

The fixtures cover ambiguous dates, explicit historical values, fresh latest values, successful refresh, failed refresh, absent canonical coverage, and blocked TC-01 coverage. Gate evidence and manifests live under `test/.evidence/gate-{2a,2b,2c}/`.

## Production vs Test Policy

The production allowlist is Bank Rate only: `uk.bank-rate-current` with the `bank-rate-latest` refresh profile. The ONSPD postcode capability, `uk.postcode-resolution` with `onspd-one-postcode`, exists only behind the hidden `test-online-v1` policy used by the Phase 2c approval gate. It is never in production.

## What this runtime does NOT do

- Answer London office rent, vacancy, news, or transaction questions. Those coverage areas are blocked.
- Provide production auth or tenancy.
- Give the model filesystem, shell, or network tools. It has only the six typed tools.
- Write canonical data. The Python data plane owns persistence.
- Run real-model smoke by default.
- Roll out ONSPD in production.

## Product coverage limit

The dashboard and runtime expose Bank Rate only. London office rent, vacancy,
news, transaction, and supply questions are finalized as explicit unavailable
coverage until their canonical source gates are approved; the UI does not
invent KPI cards for them.

## Scope Claim

a controlled Pi session and dashboard can produce a replayable,
host-hydrated, citation-grounded `market_brief.v1` with explicit coverage
limits, sourced from canonical Bank Rate.
