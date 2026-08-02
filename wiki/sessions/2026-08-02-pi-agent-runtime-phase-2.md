---
type: wiki
created: 2026-08-02
source_change: feature/pi-agent-runtime-phase-2
status: complete
tags: [agent, runtime, session]
---

# Pi Agent Runtime Phase 2 Implementation

## Outcome

Phase 2 of [[decisions/pi-agent-runtime-and-skills-vertical-slice]] is complete. Gates 2a, 2b, and 2c passed, establishing the narrow accepted claim: a controlled Pi session can produce a replayable, host-hydrated, citation-grounded `market_brief.v1` from canonical Bank Rate data with explicit partial coverage.

## What Was Built

- Added the `agent-runtime/` Node/TypeScript package with pinned Pi dependencies, boot lockdown, and six sequential typed tools: five facade tools plus `finalize_market_brief`.
- Added a bounded facade launcher using FD 3 keys, a host-owned environment, Ajv 2020 strict validation, and process-group cleanup.
- Added the runtime-only finalizer, host citation ledger with per-record lineage, and `ModelTextBuffer` numeric guard.
- Added a host-owned turn runner with 8-call, 3-poll, 2-finalize, 128 KiB, and 45 second budgets, monotonic polling, and terminal re-query.
- Added an in-memory session registry with opaque HMAC generation tags, 404 and 410 semantics, and tombstones.
- Added seven HTTP routes and authenticated SSE with ten event types, 256 event and 2 MiB replay rings, `Last-Event-ID` replay, and buffer-gated `message.delta` events.
- Added recovery, race-tested cancellation, and hidden ONSPD approval through a serialized dispatcher and `resumeTurn` continuation under a test-only policy.

## Verification

- Gates 2a, 2b, and 2c passed. Gate 2a exercised seven deterministic faux fixtures through the real `createAgentSession` path. Gate 2b covered product transport, including `published_at: null` and three-confidence fixture coverage. Gate 2c covered hidden ONSPD approval and the smoke-gate placeholder.
- Final verification waves F1 through F4 were approved across 101 disjoint primary test IDs in six gate manifests. The runtime suite passed 155 tests with two opt-in skips, fixtures passed 8 of 8, and `tsc` was clean.
- Evidence manifests live in `agent-runtime/test/.evidence/gate-{2a,2b,2c}/`. The Python baseline remained unchanged at one known failure, 380 passing tests, and 15 skipped tests.

## Known Issues

- NF-1 remains open: the Phase 1 packaged catalog locator schema only admits scalars, while the real facade emits nested locators. Phase 2 fixtures and integration use a loosened temporary catalog.
- The opt-in GLM-5.2 smoke through the user's sub2api gateway connects and drives real tools, but does not converge to a completed brief. Its strict smoke result is RED; deterministic gates remain the acceptance authority. No key was committed.

## Follow-ups

- Fix the Phase 1 catalog locator schema for nested facade locators.
- Tune real-model convergence for the GLM-5.2 smoke.
- Merge `feature/pi-agent-runtime-phase-2` into `main`.
