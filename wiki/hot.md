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

## Recent Decisions
- Proposed [[wiki/decisions/datasource-database-and-scheduled-ingestion|Datasource Database and Scheduled Ingestion Pipeline]]: original SQLite＋CAS data model, durable scheduling, evidence/revision/as-of semantics, source operating matrix, and acceptance specification.
- SQLite + immutable CAS evidence is the canonical datasource store.
- Production ingestion is bounded by registry policy and lane promotion.
- ONSPD is one-postcode on-demand only; a competition-project retention deadline
  through 2026-08-31 was explicitly approved and live capture was verified.
- Initialised git + memory structure.
- Wired remote `origin` → `ricoyudog/london_real_estate`, pushed initial commit `1d4af7f`.
- Confirmed Python stack: pure stdlib HTTP, uv + hatchling, pytest `live` marker convention.

## Architecture Pulse
- **Stable**: durable jobs, CAS evidence, isolated parsing, SQLite canonical
  reads, bounded refresh and deterministic projections.
- **Operational**: Bank Rate, ONS/Nomis macro, VOA, ONS hybrid-working and MHCLG
  EPC workflows have real evidence-to-canonical validation.
- **Policy-gated**: PLD, restricted content, BNP,
  Rightmove and GLA workflows remain blocked or manual review.
- **Evolving**: Agent Skill and Tool research, plus source-policy/product-coverage delivery.
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
- Phase 2 Pi Agent Runtime complete: 2a, 2b, and 2c gates passed; `agent-runtime/`
  Node package; 142 tests; known Phase 1 catalog NF-1.
