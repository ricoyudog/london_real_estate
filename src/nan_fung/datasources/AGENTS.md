## OVERVIEW

Legacy `SourceResult` adapters over the shared `acquire` boundary.
Canonical ingestion goes through `ingestion/` workflows, not this directory.

## STRUCTURE

| File | Role |
| --- | --- |
| `__init__.py` | Public legacy fetcher exports. |
| `common.py` | HTTP acquisition, policies, redaction, throttling, result helpers. |
| `catalog.py` | Static legacy-fetcher and workflow-coverage catalogue. |
| `macro.py` | BoE, ONS, and Nomis macroeconomic adapters. |
| `market.py` | Public office reports and VOA stock adapters. |
| `esg.py` | Non-domestic EPC indicator adapter. |
| `news.py` | GOV.UK market-news discovery and content adapters. |
| `planning.py` | Planning London Datahub application and search adapters. |
| `geography.py` | ONS postcode and GLA town-centre adapters. |
| `hybrid.py` | ONS hybrid-working indicator adapter. |
| `AGENTS.md` | Local datasource contribution guide. |

## ACQUIRE CONTRACT

- `SourcePolicy` is mandatory.
- Restrict requests with an explicit host allowlist.
- `max_bytes` is capped by the policy artifact limit.
- Redirects stop at the policy redirect limit.
- Respect `Retry-After` through the host gate.
- Reject URL userinfo.
- Request headers must be in `policy.allowed_request_headers`.
- Validate each response with `validate_artifact_bytes`.

## DOMAIN FETCHERS

| Module | Domain | What it fetches and how |
| --- | --- | --- |
| `macro.py` | UK macroeconomics | BoE, ONS, and Nomis releases through scoped official-source policies. |
| `market.py` | London office market | BNP report PDFs and VOA stock ZIPs, with validated parsers. |
| `esg.py` | Energy efficiency | GOV.UK EPC Table A ODS, retained as an all-non-domestic proxy. |
| `news.py` | Market news | GOV.UK Search API discovery and Content API records under separate policies. |
| `planning.py` | Supply pipeline | Planning London Datahub application and bounded Elasticsearch search requests. |
| `geography.py` | Location geography | ONSPD postcode and ArcGIS town-centre pages with stable IDs. |
| `hybrid.py` | Working patterns | ONS working-arrangements XLSX, retained as a Great Britain proxy. |

## WHERE TO LOOK

| Task | Location |
| --- | --- |
| Add a new domain fetcher | New module here, export in `__init__.py`, then bind canonical work in `ingestion/`. |
| Change HTTP policy enforcement | `common.py`, `acquire`, `acquire_to_artifact`, and `ingestion/policies.py`. |
| Change redaction | `common.py`, `redact_headers`, `redact_url`. |
| Change host throttle | `common.py`, `_throttle_host`, and the `HostRequestGate` caller. |

## ANTI-PATTERNS (THIS DIRECTORY)

- Do not call fetchers from canonical ingestion paths. Bind through a workflow in `ingestion/`.
- Do not bypass `acquire`. Policy enforcement is mandatory.
- Do not pass credentials. `acquire` stores none and returns none in metadata.
- Do not lift the `max_bytes` cap without source-policy review.
- Do not follow redirects to non-allowed hosts. `validate_target` runs before every hop.
