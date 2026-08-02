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

Set these values before booting a session:

```sh
export PI_MODEL=provider/model
export CRE_DATA_DIR=/absolute/path/to/seeded-store
```

- `PI_MODEL=provider/model` is required and fails fast when missing, empty, malformed, unavailable, or unauthorized.
- `CRE_DATA_DIR=<absolute path to the seeded store>` is required because the child facade launcher uses it.
- `RUN_REAL_MODEL_SMOKE=1` opts into the smoke gate. When unset, that test skips. It never replaces the deterministic gates.

## Test

Run the top-level suites:

```sh
npm test
```

Run the seven deterministic `fauxProvider` fixtures through the real `createAgentSession` path:

```sh
node --experimental-strip-types --test test/fixtures/*.test.ts
```

The fixtures cover ambiguous dates, explicit historical values, fresh latest values, successful refresh, failed refresh, absent canonical coverage, and blocked TC-01 coverage. Gate evidence and manifests live under `test/.evidence/gate-{2a,2b,2c}/`.

## Production vs Test Policy

The production allowlist is Bank Rate only: `uk.bank-rate-current` with the `bank-rate-latest` refresh profile. The ONSPD postcode capability, `uk.postcode-resolution` with `onspd-one-postcode`, exists only behind the hidden `test-online-v1` policy used by the Phase 2c approval gate. It is never in production.

## What this runtime does NOT do

- Answer London office rent, vacancy, news, or transaction questions. Those coverage areas are blocked.
- Provide a dashboard.
- Provide production auth or tenancy.
- Give the model filesystem, shell, or network tools. It has only the six typed tools.
- Write canonical data. The Python data plane owns persistence.
- Run real-model smoke by default.
- Roll out ONSPD in production.

## Known Limitation

NF-1: the packaged Phase-1 contract catalog's locator schema admits only scalar values, but the real facade emits nested locator objects. The launcher therefore uses a loosened temporary catalog copy in fixtures and integration. This is a deferred Phase-1 fix.

## Scope Claim

a controlled Pi session can produce a replayable, host-hydrated, citation-grounded market_brief.v1 with explicit partial coverage, sourced from canonical Bank Rate.
