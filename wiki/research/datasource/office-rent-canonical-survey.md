---
type: wiki
updated: 2026-08-04
source: "[[wiki/research/datasource/01-office-rent|辦公室租金 Data Sources]]"
tags: [research, datasource, canonical-eligibility, ogl, voa, vendor-copyrighted]
---

# London Office Rent Canonical-Eligibility Survey — 2026-08-04

## Why this survey

The original [[wiki/research/datasource/01-office-rent|辦公室租金 research]]
established `report-derived` access via BNP Paribas Real Estate quarterly PDFs.
That gives us evidence-grade material but **not** a canonical production
source under OGL v3.0. The PoC's biggest coverage gap is Prime / Grade A /
achieved office rent at submarket granularity. This page documents whether any
public OGL source can close that gap.

## Critical metric distinctions

| Concept | Definition | Origin |
|---|---|---|
| **Prime rent** | Vendor headline: best space, best quality, best submarket | Vendor methodology, varies by provider |
| **Grade A rent** | Vendor building-quality classification | Vendor methodology, no statutory equivalent |
| **Average achieved rent** | Transaction-level rent actually agreed | Only available in vendor / paid databases |
| **Rateable value (VOA)** | Statutory assessment of open-market rental value at the antecedent valuation date | Used for non-domestic rating; *not* market rent |

The four concepts are **not interchangeable**. Cross-vendor "Prime" comparisons
require methodology alignment; rateable value cannot be mapped to any of the
three market-rent metrics without explicit modelling — and doing so silently
is the most common data-integrity error in this domain.

## What was probed

Verified by visiting each URL on 2026-08-04:

### Candidate 1: ONS Index of Private Housing Rental Prices (IPHRP)

- URL: <https://www.ons.gov.uk/economy/inflationandpriceindices/methodologies/indexofprivatehousingrentalpricesqmi>
- Successor PIPR: <https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/priceindexofprivaterentsukmonthlypricestatistics>
- **License:** OGL v3.0
- **Cadence:** monthly
- **Coverage:** residential only (houses, flats, furnished/unfurnished). Methodology explicitly excludes commercial property.
- **Verdict:** rejected for office rent. May be canonical for residential rent — never relabelled as office.

### Candidate 2: ONS commercial rent / "Rental Fairness Office" series

- Closest existing ONS series: UKEA ROCP
  (<https://www.ons.gov.uk/economy/grossdomesticproductgdp/timeseries/rocp/ukea>)
  — annual UK aggregate £ millions rent paid by private non-financial
  corporations. Not per-sqft, not London-specific, not office-specific.
- ONS "achieved rents" wording appears only in **residential** contexts.
- **License:** OGL v3.0
- **Verdict:** rejected for office rent. Eligible only as macroeconomic context.

### Candidate 3: VOA Non-Domestic Rating (the critical case)

- Downloads: <https://voaratinglists.blob.core.windows.net/html/rlidata.htm>
- Specification: <https://voaratinglists.blob.core.windows.net/html/documents/Compiled%20Rating%20List%20and%20Summary%20Valuation%20Data%20Specification.pdf>
- Statistics collection: <https://www.gov.uk/government/collections/valuation-office-agency-non-domestic-rating-statistics>

**License conflict (very important):**

- The generic GOV.UK footer says OGL v3.0 applies "except where otherwise stated."
- The VOA Rating List Downloads page states verbatim:
  *"An open government licence does not apply."*
- The rating-list terms restrict use to NDR purposes, prohibit onward
  disclosure, and require deletion when no longer needed for the permitted
  business purpose. Crown copyright and database rights are reserved.
- **This dataset-level restriction overrides the generic GOV.UK footer.**
- Note: the **aggregated statistical releases** (Stock of Properties,
  Business Floorspace) *are* OGL v3.0; the **raw rating list** is not.

**Rateable value vs market rent:**

VOA's specification defines rateable value as the *"assessment of the
open-market rental value of the hereditament on the prescribed valuation
date."* That is a statutory valuation fixed to an **antecedent** valuation
date (e.g. the 2023 list is anchored to April 2021 conditions). It is not:

- the current market rent;
- a Prime or Grade A rent;
- a transaction-level achieved rent;
- comparable across list years without an explicit revaluation framework.

**Office classification:** VOA "bulk class" includes offices, with secondary
description, special category code, broad/detailed property type, valuation
scheme reference. None of these equals the vendor "Grade A" classification.

**Cadence:** epoch full-list files roughly every 2 months; change files twice weekly.

**Geography:** billing authority / address / region. **No** City / West End /
Canary Wharf / Midtown / Fringe submarket — those would require a separately
maintained geospatial mapping layer.

**Verdict:** partial.

- Rejected as a canonical **office market rent** source.
- Eligible as a separately named `non_domestic_rating.rateable_value`
  series **only if** the restricted VOA licence is reviewed and the production
  lane is explicitly bounded to NDR-permitted use.
- Must not be stored as `office_market_rent` / `prime_rent` / `grade_a_rent`
  / `average_achieved_rent`.

### Candidate 4: Major vendor reports (CoStar / JLL / CBRE / Savills / Knight Frank / Cushman & Wakefield)

- All six publish quarterly Central London office reports with Prime, Grade A,
  achieved, take-up, availability, supply-pipeline figures at vendor submarket
  granularity.
- All six are **vendor-copyrighted**. Public pages and PDFs carry terms
  restricting copying, redistribution, scraping, and commercial reuse. CoStar
  served HTTP 403 on direct inspection during this survey.
- **No unrestricted public API identified.** Access behind registration,
  paywall, or commercial licence.
- **Verdict:** rejected as OGL canonical source. Remain in `report-derived`
  lane with citation-only retention, exactly as BNP Paribas is handled today.

### Candidate 5: MSCI / IPD / Estama

- MSCI/IPD real-estate indices are subscription products under proprietary
  terms (<https://www.msci.com/data-and-analytics/real-estate>,
  <https://www.msci.com/legal/terms-of-use>). Copying, derivative use,
  redistribution, resale, and competing-service use are restricted unless
  covered by an Order Form.
- No OGL public office-rent dataset identified from Estama.
- **Verdict:** rejected. Use under `licensed-commercial` lane only with an
  explicit contract that permits the intended storage and downstream use.

### Candidate 6: GLA London Datastore

- Portal: <https://data.london.gov.uk/>
- Terms: <https://data.london.gov.uk/about/terms-and-conditions> — permits
  reuse but does **not** grant a blanket OGL v3.0; dataset-level rights must
  be checked individually.
- Search for `office rent`, `commercial rent` returned **no qualifying
  dataset**. The relevant office-rent data appears to live exclusively in
  vendor research, not in any GLA-owned structured rent series.
- Older query URLs (`/dataset?query=office%20rent`) now return HTTP 410;
  current portal uses `/search?type=dataset`.
- **Verdict:** rejected — no qualifying dataset found. Useful as a discovery
  layer; if a dataset appears, eligibility must be checked at resource level.

### Candidate 7: MHCLG / DLUHC

- Live tables on rents, lettings and tenancies
  (<https://www.gov.uk/government/statistical-data-sets/live-tables-on-rents-lettings-and-tenancies>):
  residential/social housing only.
- Live tables on commercial and industrial floorspace and rateable value
  (<https://www.gov.uk/government/statistical-data-sets/live-tables-on-commercial-and-industrial-floorspace-and-rateable-value-statistics>):
  historical 1998–2008 office-premises tables (P404/P405/P406) at Government
  Office Region level. Superseded by VOA responsibility.
- Planning statistics PS1/PS2: counts only, no rent.
- **License:** OGL v3.0 unless otherwise stated.
- **Verdict:** rejected for office rent. Historical commercial tables may
  serve as OGL context data only.

### Candidate 8: Eurostat HICP actual rentals (CP041)

- API: <https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_midx?geo=UK&coicop=CP041>
- **License:** EU reuse framework.
- **Coverage:** residential housing rent inflation, UK national.
- **Verdict:** rejected. May serve as a residential rent inflation
  comparator, never as a commercial-office metric.

## Final summary table

| Source | License | Granularity | Cadence | Canonical verdict |
|---|---|---|---|---|
| ONS IPHRP / PIPR | OGL v3.0 | UK / region / LA, residential | Monthly | **Rejected** (residential only) |
| ONS UKEA ROCP | OGL v3.0 | UK national, £ aggregate | Annual | **Rejected** (macro context only) |
| VOA Rating List Downloads | **Restricted — not OGL** | Billing authority / address | ~2-monthly epoch + twice-weekly change | **Partial** — `rateable_value` only, never market rent |
| VOA statistical releases (Stock / Floorspace) | OGL v3.0 | LA / region / SCat | Annual | **Partial** — stock baseline only |
| MHCLG historical commercial tables | OGL v3.0 | Government Office Region | Annual (1998–2008) | **Rejected** (superseded, not current rent) |
| GLA London Datastore | Mixed (per-dataset) | Borough | Dataset-specific | **Rejected** — no qualifying office-rent dataset |
| CoStar / JLL / CBRE / Savills / Knight Frank / Cushman & Wakefield | Vendor copyright | Vendor submarkets | Quarterly | **Rejected** — `report-derived` only |
| MSCI / IPD | Subscription / proprietary | Subscription-specific | Product-specific | **Rejected** — `licensed-commercial` only |
| Estama | Commercial / unclear | Not verified | Not verified | **Rejected / unresolved** |
| Eurostat HICP CP041 | EU reuse | UK national | Monthly | **Rejected** (residential only) |

## Bottom line

> **No OGL-licensed government dataset publishes London office market rents
> at submarket granularity. The required Prime / Grade A / achieved-rent
> series remains a commercial-source problem, not an open-data problem.**

The `london-office-rent` capability stays blocked with explicit reference to
this survey, mirroring how `london-project-supply` stays blocked with
reference to
[[wiki/research/datasource/planning-data-gov-uk-survey|planning.data.gov.uk survey]].

## Recommended source/metric classification

If the project ever ingests VOA data, the schema must use a separate metric
namespace — never `office_market_rent`:

```text
office_market_rent            # blocked — no canonical source
  prime_rent                  # vendor-only
  grade_a_rent                # vendor-only
  average_achieved_rent       # vendor-only

non_domestic_rating           # distinct family, restricted licence applies
  rateable_value              # GBP/year, statutory antecedent date
  rateable_value_per_area     # GBP/m²/year, derived
  rating_list_effective_date  # ISO date
  valuation_date              # statutory antecedent date
```

Lane policy:

- **`production_ingestion`** — only OGL sources; never VOA rating-list
  downloads unless legal review accepts the restricted NDR-purpose licence.
- **`report-derived`** — BNP, JLL, CBRE, Savills, Knight Frank, Cushman &
  Wakefield, CoStar. Citation-only retention; never promoted to canonical.
- **`licensed-commercial`** — MSCI/IPD, Estama, paid vendor APIs. Requires
  explicit contract permitting intended storage and downstream use.

## References

- Original research: [[wiki/research/datasource/01-office-rent|辦公室租金 Data Sources]]
- Operational status: [[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status — 2026-08-01]]
- Parallel survey: [[wiki/research/datasource/office-vacancy-canonical-survey|London Office Vacancy Canonical-Eligibility Survey — 2026-08-04]]
- Format precedent: [[wiki/research/datasource/planning-data-gov-uk-survey|planning.data.gov.uk Crown Copyright Survey — 2026-08-03]]
