---
type: wiki
updated: 2026-08-04
status: accepted
source: "[[.omo/plans/onboard-canonical-capabilities|Onboard Canonical Capabilities Plan]]"
---

# Canonical Capabilities Unlock — 2026-08-04

## Decision

Promote seven query-only canonical capabilities to `supported`, taking the
agent runtime from two to nine production capabilities. The new coverage is
UK GDP, inflation, labour, London employment, hybrid working, London office
stock, and London EPC certificates. Each capability is backed by an existing
production datasource, a manifest entry, runtime grant, skill guidance, and a
host finalizer claim guard.

## Capabilities

| capability_id | datasource_ids | numeric_value_field | query_kind | limitations |
| --- | --- | --- | --- | --- |
| `uk.gdp.current` | `ons.gdp.ecyx`, `ons.gdp.ihyq` | `value` | `metrics` | UK macro GDP, not London office |
| `uk.inflation.current` | `ons.inflation.d7g7`, `ons.inflation.l55o`, `ons.inflation.czbh` | `value` | `metrics` | UK inflation (CPIH/OOH/deflator) |
| `uk.labour.current` | `ons.labour.lf24`, `ons.labour.mgsx`, `ons.labour.ap2y`, `ons.labour.kai9` | `value` | `metrics` | UK labour market |
| `uk.employment.london` | `nomis.nm_59_1.london_lfs`, `nomis.nm_130_1.london_workforce_jobs` | `value` | `metrics` | London employment |
| `uk.hybrid-working` | `ons.opn.hybrid_working` | `estimate_percent` | `metrics` | GB survey proxy, not office occupancy |
| `london.office-stock` | `voa.ndr_office_stock` | `office_property_count` | `supply` | Annual stock count, not vacancy/floorspace |
| `london.epc-certificates` | `mhclg.epc.live_table_a_london` | `number_lodgements` | `metrics` | All non-domestic, not offices only |

## Ingestion Fixes

Phase 0 corrected the production contract before promotion. File-release
integer values are canonicalized to decimal strings, and ONS, Nomis, and
file-release records now carry `source_date` metadata. The finalizer contract
test confirms a real-shaped file-release observation reaches the facade with a
decimal string value and a string source date.

## Finalizer Guards

Phase 2.2 added capability-specific finalizer guards. They reject claims that
relabel UK macro measures as London office metrics, hybrid-working survey data
as office occupancy or demand, EPC lodgements as office-only certificates, or
office stock as vacancy, floorspace, or market rent.

## Skills Updates

Phase 2.1 updated the macro, ESG, office-demand, and office-supply skills to
describe the new capabilities and their limits. The checked-in skills manifest
was regenerated so runtime boot verifies the revised prompt files.

## Verification Status

Python capability and query tests are green: `109 passed`, including 50
capability/query tests. TypeScript typecheck is green. Real-GLM UI tests remain
pending a user-provided `agent-runtime/.env`; no live-model result is claimed
by this decision.

## References

- [[.omo/plans/onboard-canonical-capabilities|Onboard Canonical Capabilities Plan]]
- High-accuracy review sessions: Momus `ses_0345b5379ffeHpIWWRh6HfOkqF` and Oracle `ses_0345aec05ffeuUn9kmxQM4kTDb`
- [[wiki/research/datasource/office-rent-canonical-survey|London Office Rent Canonical-Eligibility Survey]]
- [[wiki/research/datasource/office-vacancy-canonical-survey|London Office Vacancy Canonical-Eligibility Survey]]
- [[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status]]
