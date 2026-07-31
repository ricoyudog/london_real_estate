---
type: memory
created: 2026-07-31
---

# MEMORY — Hard Constraints

> AI agent must obey these every session. Never expires.

## Project Identity
- **Name**: nan_fung (repo: london_real_estate)
- **Purpose**: Free datasource tools for London office market research — pure-stdlib Python fetchers that wrap public data (Bank of England, ONS, VOA, EPC, planning portals, news, geography) into a uniform `SourceResult` envelope for downstream research workflows.
- **Stack**: Python ≥ 3.11, uv for deps, hatchling build, pytest + ruff, pure stdlib HTTP (urllib, no requests/httpx). Skill agents under `skills/<domain>/`.

## Hard Constraints
- **Pure stdlib for HTTP** — no `requests`, `httpx`, or aiohttp in runtime deps. Use `urllib.request` via `datasources/common.py` helpers (`get_bytes`, `get_json`).
- **Every datasource returns `SourceResult`** — built via `common.source_result(...)` with category, source, source_url, retrieved_at, published_at, source_updated_at, records.
- **`live` pytest marker** — tests hitting real network endpoints must be marked `@pytest.mark.live` so the default suite stays offline and fast. Default CI runs `-m "not live"`.
- **Packages path** — `src/nan_fung/` layout, declared in `pyproject.toml` `[tool.hatch.build.targets.wheel].packages`.

## Preferences
- All fetchers live in `src/nan_fung/datasources/` and re-export from `__init__.py` `__all__`.
- Domain skills live in `skills/<skill-name>/` with `SKILL.md` + `agents/` — 7 skills mirror the 7 research workflows (office demand, ESG, market metrics, geography, news, supply, macro).
- One module per datasource domain (macro.py, market.py, esg.py, news.py, planning.py, geography.py, hybrid.py).
- Tests in `tests/test_<domain>.py`, one per datasource module.
- User-Agent string centralised: `nan-fung-datasource-research/0.1` in `common.py`.
