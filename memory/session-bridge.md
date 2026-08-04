---
type: memory
updated: 2026-08-04
---

# Session Bridge

> AI agent reads this first at startup. Last session's handoff state.

## Active corgi Change
- **Change**: none
- **Phase**: none
- **Branch**: main (tracking origin/main, HEAD `a5c399e`)

## Done (2026-08-04 london-planning-activity agent capability)
- Delivered the full agent-facing `london-planning-activity` capability (commit `a5c399e`): City authority `203` geography filter, host-derived facade binding metadata, trusted finalizer with planning proxy enforcement, production grant, deterministic Pi/browser fixtures, gated real GLM + real-browser E2E.
- Redesigned the streaming NumericGuard: it now filters numeric tokens from streamed prose instead of killing the turn. Draft-level `guardModelText` remains the authoritative numeric boundary. This unblocks GLM-5.2's natural narration while the host retains exclusive artifact number ownership.
- Widened `isNegatedClaim` to accept `rather than`, `not X-specific Y`, and wider clause-level negation patterns. Planning-backed drafts can now correctly say "not office-specific supply" without being rejected.
- Real GLM-5.2 verified: tool sequence `describe → query → citation → finalize` executes correctly through the real FacadeLauncher subprocess. Remaining turn-level flakiness is GLM-5.2 draft-format variability (sometimes adds `text` to numeric facts → `SCHEMA_ESCAPE`), not host code.
- Updated all four wiki pages (hot, index, log, decision) and this bridge.

## Done (2026-08-03 ingestion unlock)
- `pld.applications_search` promoted to production on Crown-copyright `planning.data.gov.uk` (OGL v3); `e91620e` merged; 189 Camden canonical observations ingested in live daemon run.
- `london-project-supply` stays blocked — no public source supplies floorspace values.

## Done (2026-08-02 full-stack delivery)
- Docker one-command fixture demo: migration/seed/marker verification, named-volume persistence, health-gated Node startup, fail-closed non-demo marker handling.
- Same-origin UI: fixture/runtime identity, cancel/retry, authenticated pagehide cleanup, safe failures, freshness/confidence/lineage/source links.
- Real `glm/GLM-5.2` passed Bank Rate E2E and Chinese unavailable TC-01.
- Final deterministic gates (pre-planning): Python `387 passed, 15 deselected`; Node `180 passed, 2 skipped`; Playwright/axe `9 passed`.

## Waiting (next steps / blockers)
- `london-prime-rent`, `london-office-vacancy`, `uk-investment-transactions`, `uk-ranked-market-news` require approved canonical evidence before their dashboard cards become answerable.
- GLM-5.2 draft-format variability: the model occasionally adds `text` to numeric facts or uses non-standard inference shapes. The host finalizer correctly rejects these (`SCHEMA_ESCAPE`/`PLANNING_PROXY_CLAIM`), but the model does not always self-correct within the turn budget. This is a model-compliance limitation, not a code defect.
- WSL2 9P mount (`/mnt/e`) causes disk I/O stalls that exceed test timeouts under load; real-GLM and Playwright suites are more reliable on native Linux or macOS.

## Pitfalls
- `.codegraph` is a **symlink** to `~/.omo/codegraph/...` — must stay in `.gitignore`.
- A host-emitted cancelled SSE terminal means the completed runner must not replay earlier batched tool events; doing so raises `SseProtocolError`.
- Linux cannot execute fixture ingestion commands that require macOS `sandbox-exec`; Docker demo seed accepts only the checksum-pinned packaged fixture.

## Next Session Start
1. Read this file ← you are here
2. Read [[wiki/hot]]
3. Read [[wiki/index]]
4. Then `wiki/User Requirement.md` and `wiki/research/datasource/` for project intent
