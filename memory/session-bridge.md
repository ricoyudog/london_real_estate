---
type: memory
updated: 2026-08-02
---

# Session Bridge

> AI agent reads this first at startup. Last session's handoff state.

## Active corgi Change
- **Change**: none
- **Phase**: none
- **Branch**: main (tracking origin/main)

## Done (2026-08-02 full-stack delivery)
- Docker now provides a one-command deterministic fixture demo: one-shot migration/seed/marker verification, named-volume persistence, health-gated Node startup, and fail-closed non-demo marker handling. Linux never runs the ingestion daemon.
- The same-origin UI exposes fixture/runtime identity, cancel/retry, authenticated pagehide cleanup, safe failures, freshness/publication/confidence/lineage/source links, and no approval controls or streaming model facts.
- Real `glm/GLM-5.2` passed CLI and in-app browser acceptance through Pi `createAgentSession`, typed Facade tools, SQLite canonical data and the host finalizer. Bank Rate completed with citation lineage; overview was partial; West End vacancy was unavailable without a fabricated number.
- Cancel now races a stalled Pi prompt, aborts promptly, skips late lifecycle projection, records `terminal_state: cancelled`, releases the turn, and accepts an immediate retry.
- Final deterministic gates: Python `387 passed, 15 deselected`; Node `182` tests (`180 passed, 2 skipped`), typecheck and production dependency audit green; Playwright/axe `9 passed`; real-model gate passed; wheel/deck/Docker verification recorded in `tests/Test case.md` and `wiki/questions/Test_result/`.
- Preserved and visually verified the final six-slide architecture/demo PPTX; removed its generated inspect dump, duplicate test catalog and unreferenced screenshot.

## Done (historical)
- Phase 2 Pi Agent Runtime (2a/2b/2c) complete and merged to `main`; all mandatory gates passed.
- Authored [[wiki/decisions/datasource-database-and-scheduled-ingestion]] and indexed it under `wiki/decisions/`.
- Decision covers the full target: versioned datasource/source registry, SQLite＋CAS, durable attempts/scheduler/watermarks, immutable evidence/revisions, correct latest/as-of, manual review, all datasource schedules/backfills, access/retention, read API, operations, a Phase 0 governance gate and 12 delivery phases.
- Architecture, codebase and source-research reviews were applied; late-approval regression, cross-stream FKs, replay versioning, snapshot deletion scope, manual-promotion provenance and legal gates are explicit.
- Prior baseline remains initial commit `1d4af7f`.
- Initialised git in this folder, wired `origin` → `ricoyudog/london_real_estate`
- Discovered existing Python codebase (was NOT empty — `src/nan_fung/datasources/` + 7 skills + 31 tests already present)
- Expanded `.gitignore` (Python/venv/caches/tooling state), verified `pytest -m "not live"` → 18 passed
- Updated `openspec/config.yaml` (provider: github, real description) and `memory/MEMORY.md` (real stack + constraints)
- Initial commit `1d4af7f` pushed to `origin/main` — 71 files, 4059 insertions

## Waiting (next steps / blockers)
- Only UK Bank Rate is current product coverage. Prime rent, vacancy, transactions and ranked news require approved canonical evidence before their dashboard cards become answerable.
- Historical datasource-planning note: decision remained `status: proposed`; run Phase 0 source approvals, then Phase 1 executable DDL/lifecycle/clean-wheel spike before changing it to `accepted`.
- PLD, MPC content, BNP and Rightmove cannot production-promote until their named licence/retention gates pass; engineering completion must retain blocked/degraded behavior.
- Consider setting `git config user.name` / `user.email` globally — current commits use auto-detected `chunsing yu <chunsingyu@chunsingdeMac-mini.local>`.

## New Pitfalls
- `.codegraph` is a **symlink** to `~/.omo/codegraph/...` — local tooling state, must stay in `.gitignore`. It got staged on first `git add -A` because gitignore didn't apply retroactively; had to `git rm --cached .codegraph` before commit. Watch for this on fresh repos.
- A host-emitted cancelled SSE terminal means the completed runner must not replay any earlier batched tool events; doing so raises `SseProtocolError` and can mislabel the safe cancel as `RUNTIME_UNAVAILABLE`.
- Linux cannot execute the normal fixture ingestion command because parser isolation correctly requires macOS `sandbox-exec`. Docker demo seed accepts only the checksum-pinned packaged fixture and persists it through the trusted `OperationalStore` boundary; arbitrary evidence parsing remains forbidden.

## New Discoveries
- The offline Python gate collects 402 tests and runs 387 after marker exclusion; Node's top-level and nested fixtures total 182 tests.
- Legacy `SourceResult` float／`+00:00` contracts need adapters while persisted v1 uses decimal strings／`Z`.
- There is no safe claim of complete PLD future supply until multi-window pagination/detail reconciliation and source rights both pass.
- Existing codebase is richer than the memory templates assumed: `wiki/research/datasource/` has 13 numbered research notes, plus `wiki/User Requirement.md`, `wiki/Technical Test for Forward Deployed Engineer.pdf`, and `wiki/rearch/UI/chatbot-dashboard-decision.md`. These all committed cleanly.
- Tests use `live` marker for network tests — default suite runs offline and fast.

## Next Session Start
1. Read this file ← you are here
2. Read [[wiki/hot]]
3. Read [[wiki/index]]
4. Then `wiki/User Requirement.md` and `wiki/research/datasource/` for project intent
