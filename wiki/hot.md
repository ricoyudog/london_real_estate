---
type: wiki
updated: 2026-08-01
tags: [hot, entry]
pinned: true
---

# Hot — nan_fung Latest

> ~500 words | Hard cap 600 words | Updated every session | First entry point for humans and AI

## Active Changes
- Datasource operational system implemented on `feat/datasource`; remaining work is
  source-governance approval and product-coverage delivery, not another ad-hoc scraper.

## Recent Decisions
- SQLite + immutable CAS evidence is the canonical datasource store.
- Production ingestion is bounded by registry policy and lane promotion.
- ONSPD is one-postcode on-demand only and requires an explicit human-approved
  retention deadline before live capture.

## Architecture Pulse
- **Stable**: durable jobs, CAS evidence, isolated parsing, SQLite canonical
  reads, bounded refresh and deterministic projections.
- **Operational**: Bank Rate, ONS/Nomis macro, VOA, ONS hybrid-working and MHCLG
  EPC workflows have real evidence-to-canonical validation.
- **Policy-gated**: ONSPD retention approval; PLD, restricted content, BNP,
  Rightmove and GLA workflows remain blocked or manual review.

## Recent Pitfalls
- `.codegraph` symlink staged on first `git add -A` despite gitignore — see [[memory/pitfalls]] when added

## Recently Shipped
- `1e3e7b4` durable ingestion foundation
- `b4291f0` source adapters and daemon supervisor
- `b8453f8` operator APIs, projections and delivery controls
- `0511da9` live Bank Rate canonical-persistence test
- `a20738e` current official file-release support
