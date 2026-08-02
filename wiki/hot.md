---
type: wiki
updated: 2026-08-02
tags: [hot, entry]
pinned: true
---

# Hot — nan_fung Latest

> ~500 words | Hard cap 600 words | Updated every session | First entry point for humans and AI

## Active Changes
- Datasource operational system merged to `main`; remaining work is
  source-governance approval and product-coverage delivery, not another ad-hoc scraper.
- Runtime/dashboard delivery is complete: an unqualified date now reaches Pi for the latest canonical view; the browser and Docker service visibly preserve unavailable coverage rather than inventing market values.

## Recent Decisions
- Proposed [[wiki/decisions/datasource-database-and-scheduled-ingestion|Datasource Database and Scheduled Ingestion Pipeline]]: original SQLite＋CAS data model, durable scheduling, evidence/revision/as-of semantics, source operating matrix, and acceptance specification.
- SQLite + immutable CAS evidence is the canonical datasource store.
- Production ingestion is bounded by registry policy and lane promotion.
- ONSPD is one-postcode on-demand only; a competition-project retention deadline
  through 2026-08-31 was explicitly approved and live capture was verified.
- Initialised git + memory structure.
- Wired remote `origin` → `ricoyudog/london_real_estate`, pushed initial commit `1d4af7f`.
- Confirmed Python stack: pure stdlib HTTP, uv + hatchling, pytest `live` marker convention.
- Agent date handling: no host-side date clarification gate; unqualified questions retain the canonical `as_of` / freshness contract.

## Architecture Pulse
- **Stable**: durable jobs, CAS evidence, isolated parsing, SQLite canonical
  reads, bounded refresh and deterministic projections.
- **Operational**: Bank Rate, ONS/Nomis macro, VOA, ONS hybrid-working and MHCLG
  EPC workflows have real evidence-to-canonical validation.
- **Policy-gated**: PLD, restricted content, BNP,
  Rightmove and GLA workflows remain blocked or manual review.
- **Delivered**: Pi runtime, typed Facade boundary, same-origin dashboard and Docker read service. Product coverage remains deliberately narrower than engineering workflow coverage.
- **Legacy**: direct `SourceResult` fetchers remain compatibility adapters, not canonical ingestion.

## Recent Pitfalls
- `.codegraph` symlink staged on first `git add -A` despite gitignore — see [[memory/pitfalls]] when added

## Recently Shipped
- `1e3e7b4` durable ingestion foundation
- `b4291f0` source adapters and daemon supervisor
- `b8453f8` operator APIs, projections and delivery controls
- `0511da9` live Bank Rate canonical-persistence test
- `a20738e` current official file-release support
- `3633595` ONSPD refresh budget and second confirmation
- `620b5c9` live ONSPD retention drill
- 2026-08-02 runtime repair: real GLM Bank Rate E2E and Chinese unavailable TC-01 passed; citation locator contract is fixed.
- 2026-08-02 dashboard / Docker: actual browser screenshots and seeded-container TC-01 pass; `docs/London-Market-Desk-Architecture-and-Demo-2026-08-02.pptx` delivered.
