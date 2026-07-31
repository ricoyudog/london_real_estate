---
type: wiki
updated: 2026-07-31
tags: [hot, entry]
pinned: true
---

# Hot — nan_fung Latest

> ~500 words | Hard cap 600 words | Updated every session | First entry point for humans and AI

## Active Changes
- (No active change yet — run `/corgi-gh-propose` since tracking provider is GitHub)

## Recent Decisions
- Initialised git + memory structure
- Wired remote `origin` → `ricoyudog/london_real_estate`, pushed initial commit `1d4af7f`
- Confirmed Python stack: pure stdlib HTTP, uv + hatchling, pytest `live` marker convention

## Architecture Pulse
- **Stable**: `src/nan_fung/datasources/` — 8 modules returning `SourceResult` envelope via `common.py` helpers (`get_bytes`, `get_json`, `source_result`)
- **Evolving**: 7 domain skills under `skills/`; 13 datasource research notes under `wiki/research/datasource/`
- **Legacy**: None identified

## Recent Pitfalls
- `.codegraph` symlink staged on first `git add -A` despite gitignore — see [[memory/pitfalls]] when added

## Recently Shipped
- Initial commit (71 files) — full Python codebase, tests, skills, wiki research notes
