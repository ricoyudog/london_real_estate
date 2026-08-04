---
type: wiki
updated: 2026-08-04
tags: [hot, entry]
pinned: true
---

# Hot — nan_fung Latest

> ~500 words | Hard cap 600 words | Updated every session | First entry point for humans and AI

## Active Changes
- Two product capabilities are now live: **UK Bank Rate** and
  **borough/month planning-application activity**. The dashboard answers both
  with host-finalized, citation-grounded briefs.
- The runtime's streaming NumericGuard was **redesigned** (2026-08-04):
  it now filters numeric tokens from streamed prose instead of killing the
  entire turn. The draft-level guard still rejects numbers in title/facts/
  inferences/caveats/limitations. This lets GLM-5.2 narrate naturally while
  the host retains exclusive ownership of all numeric values in the artifact.

## Recent Decisions
- [[wiki/decisions/london-planning-activity-unlock-2026-08-03|London Planning Activity Unlock — 2026-08-03]] — accepted; `pld.applications_search` promoted to production; live daemon run ingested 189 Camden canonical observations.
- [[wiki/decisions/datasource-database-and-scheduled-ingestion|Datasource Database and Scheduled Ingestion Pipeline]] — original proposed specification; current status is `accepted` via the implementation record.
- SQLite + immutable CAS evidence is the canonical datasource store.
- Production ingestion is bounded by registry policy and lane promotion.
- ONSPD is one-postcode on-demand only; a competition-project retention deadline through 2026-08-31 was explicitly approved and live capture was verified.
- Initialised git + memory structure; wired `origin` → `ricoyudog/london_real_estate`.
- Agent date handling: no host-side date clarification gate; unqualified questions retain the canonical `as_of` / freshness contract.

## Architecture Pulse
- **Stable**: durable jobs, CAS evidence, isolated parsing, SQLite canonical reads, bounded refresh, deterministic projections.
- **Operational**: Bank Rate, ONS/Nomis macro, VOA, ONS hybrid-working, MHCLG EPC, and PLD planning-activity workflows have real evidence-to-canonical validation.
- **Policy-gated**: restricted content, BNP, Rightmove and GLA workflows remain blocked or manual review. `london-project-supply` stays blocked — no public source supplies floorspace values.
- **Delivered**: Pi runtime, typed Facade boundary with host-authoritative capability binding, same-origin dashboard, reproducible Docker fixture initializer, browser regression suite, real GLM-5.2 acceptance for both Bank Rate and planning activity.
- **Security boundary**: host finalizer owns all numeric values via `guardModelText` + `resolveReferences` + `guardPlanningClaims`. Streaming prose is filtered (not killed) to avoid blocking model conversation while preventing numeric hallucination in artifacts.

## Recent Pitfalls
- `.codegraph` symlink staged on first `git add -A` despite gitignore — see [[memory/pitfalls]].
- GLM-5.2 naturally narrates numbers ("the answer is 2…") in streamed prose before calling `finalize_market_brief`. The old streaming guard killed the turn; the new design filters those chunks and lets the turn complete. The draft-level guard remains the authoritative numeric safety boundary.
- WSL2 9P mount (`/mnt/e`) causes severe disk I/O stalls that can exceed test timeouts. Real-GLM and Playwright suites are more reliable on native Linux or macOS.

## Recently Shipped
- `a5c399e` feat: unlock london-planning-activity capability and relax streaming numeric guard — the full agent-facing capability: City authority 203 geography filter, host-derived facade binding metadata, trusted finalizer with planning proxy enforcement, production grant, deterministic Pi/browser fixtures, gated real GLM + real-browser E2E, and the streaming guard redesign.
- `e91620e` feat(ingestion): unlock london-planning-activity on Crown-copyright planning.data.gov.uk
- `804c073` fix(agent-runtime): configure GLM tool compatibility
- `1e3e7b4` durable ingestion foundation
- `b4291f0` source adapters and daemon supervisor
- `b8453f8` operator APIs, projections and delivery controls
- 2026-08-02 runtime repair: real GLM Bank Rate E2E passed; citation locator contract fixed.
- 2026-08-02 dashboard / Docker: Bank Rate, partial overview, unavailable West End and cancel/retry passed; final six-slide PPTX delivered.
