---
type: memory
updated: 2026-07-31
---

# Session Bridge

> AI agent reads this first at startup. Last session's handoff state.

## Active corgi Change
- **Change**: none
- **Phase**: none
- **Branch**: main (tracking origin/main)

## Done (last session completed)
- Phase 2 Pi Agent Runtime (2a/2b/2c) complete in worktree `feature/pi-agent-runtime-phase-2`; awaiting final wave + merge decision
- Initialised git in this folder, wired `origin` → `ricoyudog/london_real_estate`
- Discovered existing Python codebase (was NOT empty — `src/nan_fung/datasources/` + 7 skills + 31 tests already present)
- Expanded `.gitignore` (Python/venv/caches/tooling state), verified `pytest -m "not live"` → 18 passed
- Updated `openspec/config.yaml` (provider: github, real description) and `memory/MEMORY.md` (real stack + constraints)
- Initial commit `1d4af7f` pushed to `origin/main` — 71 files, 4059 insertions

## Waiting (next steps / blockers)
- No active change. Next: run `/corgi-propose` (or `/corgi-gh-propose` since provider is github) when a feature/change is scoped.
- Consider setting `git config user.name` / `user.email` globally — current commits use auto-detected `chunsing yu <chunsingyu@chunsingdeMac-mini.local>`.

## New Pitfalls
- `.codegraph` is a **symlink** to `~/.omo/codegraph/...` — local tooling state, must stay in `.gitignore`. It got staged on first `git add -A` because gitignore didn't apply retroactively; had to `git rm --cached .codegraph` before commit. Watch for this on fresh repos.

## New Discoveries
- Existing codebase is richer than the memory templates assumed: `wiki/research/datasource/` has 13 numbered research notes, plus `wiki/User Requirement.md`, `wiki/Technical Test for Forward Deployed Engineer.pdf`, and `wiki/rearch/UI/chatbot-dashboard-decision.md`. These all committed cleanly.
- Tests use `live` marker for network tests — default suite runs offline and fast.

## Next Session Start
1. Read this file ← you are here
2. Read [[wiki/hot]]
3. Read [[wiki/index]]
4. Then `wiki/User Requirement.md` and `wiki/research/datasource/` for project intent
