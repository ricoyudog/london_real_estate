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

## Done (last session completed)
- Phase 2 Pi Agent Runtime (2a/2b/2c) complete and merged to `main`; all mandatory gates passed.
- Authored [[wiki/decisions/datasource-database-and-scheduled-ingestion]] and indexed it under `wiki/decisions/`.
- Decision covers the full target: versioned datasource/source registry, SQLite＋CAS, durable attempts/scheduler/watermarks, immutable evidence/revisions, correct latest/as-of, manual review, all datasource schedules/backfills, access/retention, read API, operations, a Phase 0 governance gate and 12 delivery phases.
- Architecture, codebase and source-research reviews were applied; late-approval regression, cross-stream FKs, replay versioning, snapshot deletion scope, manual-promotion provenance and legal gates are explicit.
- Prior baseline remains initial commit `1d4af7f`; no implementation code was changed in this session.
- Initialised git in this folder, wired `origin` → `ricoyudog/london_real_estate`
- Discovered existing Python codebase (was NOT empty — `src/nan_fung/datasources/` + 7 skills + 31 tests already present)
- Expanded `.gitignore` (Python/venv/caches/tooling state), verified `pytest -m "not live"` → 18 passed
- Updated `openspec/config.yaml` (provider: github, real description) and `memory/MEMORY.md` (real stack + constraints)
- Initial commit `1d4af7f` pushed to `origin/main` — 71 files, 4059 insertions

## Waiting (next steps / blockers)
- Historical datasource-planning note: decision remained `status: proposed`; run Phase 0 source approvals, then Phase 1 executable DDL/lifecycle/clean-wheel spike before changing it to `accepted`.
- PLD, MPC content, BNP and Rightmove cannot production-promote until their named licence/retention gates pass; engineering completion must retain blocked/degraded behavior.
- Consider setting `git config user.name` / `user.email` globally — current commits use auto-detected `chunsing yu <chunsingyu@chunsingdeMac-mini.local>`.

## New Pitfalls
- `.codegraph` is a **symlink** to `~/.omo/codegraph/...` — local tooling state, must stay in `.gitignore`. It got staged on first `git add -A` because gitignore didn't apply retroactively; had to `git rm --cached .codegraph` before commit. Watch for this on fresh repos.
- `live` is registered as a pytest marker but is not excluded by default yet; use `uv run pytest -m "not live"` until Phase 1 sets target policy.

## New Discoveries
- Current suite contains 18 offline and 13 live tests; legacy `SourceResult` float／`+00:00` contracts need adapters while persisted v1 uses decimal strings／`Z`.
- There is no safe claim of complete PLD future supply until multi-window pagination/detail reconciliation and source rights both pass.
- Existing codebase is richer than the memory templates assumed: `wiki/research/datasource/` has 13 numbered research notes, plus `wiki/User Requirement.md`, `wiki/Technical Test for Forward Deployed Engineer.pdf`, and `wiki/rearch/UI/chatbot-dashboard-decision.md`. These all committed cleanly.
- Tests use `live` marker for network tests — default suite runs offline and fast.

## Next Session Start
1. Read this file ← you are here
2. Read [[wiki/hot]]
3. Read [[wiki/index]]
4. Then `wiki/User Requirement.md` and `wiki/research/datasource/` for project intent
