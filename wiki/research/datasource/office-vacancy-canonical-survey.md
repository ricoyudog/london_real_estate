---
type: wiki
updated: 2026-08-04
source: "[[wiki/research/datasource/02-office-stock-availability|辦公室存量與可用樓面 Data Sources]]"
tags: [research, datasource, canonical-eligibility, ogl, voa, local-authority, vendor-copyrighted]
---

# London Office Vacancy / Availability Canonical-Eligibility Survey — 2026-08-04

## Why this survey

The original [[wiki/research/datasource/02-office-stock-availability|存量與可用樓面 research]]
combined two non-canonical sources: BNP Paribas Real Estate PDFs
(`report-derived`) and VOA NDR Stock of Properties (OGL v3.0 baseline for
stock, **not** vacancy). The PoC's coverage gap is **vacancy / availability
rate at submarket granularity**. This page documents whether any public OGL
source can close that gap.

## Critical metric distinctions

Three concepts that **must not** be conflated:

| Concept | Definition | Where measured |
|---|---|---|
| **Availability rate** | Vendor-defined: space actively marketed for lease/sublease by agents | Proprietary CoStar/CBRE/JLL/Savills databases only |
| **Physical vacancy rate** | Floorspace physically unoccupied at a point in time | **No single UK public source measures this** |
| **Empty property relief** | Administrative status: property receiving business-rates exemption for being empty | NNDR3 aggregated returns; or borough FOI / open-data lists |

Vendor "availability" ≠ physical vacancy ≠ business-rates "empty." Each
definition captures a different subset; silent conversion between them is a
data-integrity error.

## What was probed

Verified by visiting each URL on 2026-08-04:

### Candidate 1: VOA NDR Stock of Properties

- URL: <https://www.gov.uk/government/statistics/non-domestic-rating-stock-of-properties-march-2026>
- **License:** OGL v3.0 (confirmed in statistical-release footer)
- **Cadence:** annual, 31 March snapshot published May/June; revaluation every 3 years.
- **Granularity:** Country → Region → LA district → MSOA → LSOA, by sector (Retail / Office / Industrial / Other) and Special Category code.
- **Critical finding:** VOA background notes state verbatim:
  *"Rateable properties can be occupied or vacant. This has no impact on RV, although it can affect the level of rates levied on a property. All the statistics in this release relate to rateable properties."*
- **Verdict:** partial. Canonical for **stock baseline** (count, rateable value, floorspace). **No vacancy flag** — every cell counts occupied and vacant hereditaments together.

### Candidate 2: VOA Business Floorspace

- URL: <https://www.gov.uk/government/statistics/non-domestic-rating-business-floorspace-march-2025>
- **License:** OGL v3.0
- **Cadence:** annual
- **Granularity:** LA / SCat sector / SCat code, with floorspace (m²) and RV/m² for office-property SCats
- **Verdict:** partial. Adds floorspace denominator, still no occupancy flag.

### Candidate 3: MHCLG NNDR3 — Empty Property Relief statistics

- URL: <https://www.gov.uk/government/statistics/national-non-domestic-rates-collected-by-councils-in-england-2024-to-2025>
- **License:** OGL v3.0
- **Cadence:** annual
- **Granularity:** 296 individual billing authorities in England. **Monetary aggregates only** — no property counts, no property-type breakdown, no submarket geography.
- **Coverage:** £1.276 bn empty-property relief granted across England in 2024–25. Cannot isolate office share.
- **Historical note:** A 1998/99–2004/05 "Commercial and industrial property vacancy statistics" dataset existed (compiled from NNDR3) — **discontinued** for over 20 years. ONS FOI response (<https://www.ons.gov.uk/aboutus/transparencyandgovernance/freedomofinformationfoi/vacantcommercialproperties>) confirms: *"the ONS does not hold this data."*
- **Verdict:** partial. Provides a rough administrative-vacancy signal at LA level only. Not suitable for submarket office-vacancy rates.

### Candidate 4: VOA Rating List (raw)

- URL: <https://voaratinglists.blob.core.windows.net/html/rlidata.htm>
- **License:** **Restricted — explicitly NOT OGL.** Page states *"An open government licence does not apply."*
- Contains occupancy status at hereditament level, but cannot be used as canonical because of licence restriction.
- **Verdict:** rejected. Same licence position as the rent survey — see [[wiki/research/datasource/office-rent-canonical-survey|rent survey Candidate 3]].

### Candidate 5: ONS BRES

- URL: <https://www.ons.gov.uk/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/methodologies/businessregisterandemploymentsurveybres>
- **License:** OGL v3.0 (published tables); microdata via Chancellor's Notice (£60).
- **Cadence:** annual, ~87,000 businesses sampled.
- **Granularity:** 5-digit SIC 2007 down to LSOA.
- **Vacancy proxy logic:** would require employment-density-per-m² model × actual occupied floorspace × VOA floorspace. Too many model assumptions; SIC mismatch rate ~7% at subclass level.
- **Verdict:** proxy-only. Useful for office-employment density context; cannot directly measure vacancy.

### Candidate 6: GLA London Datastore

- Portal: <https://data.london.gov.uk/>
- Three sub-candidates checked:
  - **Commercial & Industrial Floorspace, Borough** (<https://data.london.gov.uk/dataset/commercial-and-industrial-floorspace-borough/>) — VOA-derived stock; no vacancy. OGL v3.0.
  - **HSDS Vacancy Register** (<https://data.london.gov.uk/high-street-data-service/hsds-partnership-data/hsds-business-premises>) — **NOT OGL.** Subscriber-only, sourced from Local Data Company (Green Street). GLA pays ~£15k/year; boroughs subscribe at ~£20k saving.
  - **CAZ Office Vacancy Analysis** — GLA's own evidence base uses **CoStar** for the vacancy rate figure (Figure 3.1.2 in CAZ functions and capacity PDF). Even the GLA must rely on proprietary vendor data.
- **Verdict:** rejected. No qualifying OGL vacancy dataset exists on the Datastore.

### Candidate 7: Major vendor reports (CoStar / JLL / CBRE / Savills / Knight Frank / Cushman & Wakefield / Montagu Evans / DeVita CME)

- Public quarterly Central London office reports with headline vacancy %, availability %, take-up, supply.
- All vendor-copyrighted. Savills PDF footer:
  *"The content is strictly copyright and reproduction of the whole or part of it in any form is prohibited without written permission from Savills Research."*
- Each vendor defines "availability" differently — vendor figures are not cross-comparable without methodology alignment.
- No public API identified. CoStar served HTTP 403 on inspection.
- **Verdict:** rejected as OGL canonical. Remain in `report-derived` lane.

### Candidate 8: MHCLG Planning Statistics PS1/PS2

- URL: <https://www.gov.uk/government/statistical-data-sets/live-tables-on-planning-application-statistics>
- **License:** OGL v3.0; quarterly; LPA level.
- Counts applications and decisions; some commercial breakdown. No floorspace aggregates by use class, no occupancy dimension.
- **Verdict:** rejected for vacancy. Useful for development-pipeline tracking only.

### Candidate 9: ONS Property Unit

- ONS FOI responses (<https://www.ons.gov.uk/aboutus/transparencyandgovernance/freedomofinformationfoi/vacantcommercialproperties> and `averagenondomesticpropertyvacancyrateinukcities`):
  *"Unfortunately the ONS does not hold this data."*
- **Verdict:** rejected. No current series; historical 1998–2005 series discontinued.

### Candidate 10: Local authority business-rates open data

The most promising OGL candidate for an administrative vacancy indicator.
Several London boroughs publish business-rates account data with relief /
exemption fields:

| Borough | URL | Cadence | Schema highlights |
|---|---|---|---|
| Barnet | <https://open.barnet.gov.uk/dataset/business-rates-register-and-empty-commercial-properties-2rp1v> | Monthly | `Business Rates Register.csv` + dedicated `Empty Properties.csv` |
| Sutton | <https://www.sutton.gov.uk/businesses-and-licensing/business-rates/additional-foi-information-business-rates-nndr> | 6-monthly (next Dec 2026) | Full NNDR account list with `current relief(s)`, `current exemption(s)`, valuation analysis code |
| Camden | <https://opendata.camden.gov.uk/Business-Economy/Camden-Non-Domestic-Rates-Charges-and-Reliefs/xcqw-xady> | Continuous via Socrata API | Charges + reliefs since 1 April 2010; full Socrata API (JSON/CSV/GeoJSON) |
| Waltham Forest | <https://www.walthamforest.gov.uk/index.php/businesses/business-rates/freedom-information-data> | Periodic | `empty exemptions`, `empty reliefs` columns |
| Lambeth | <https://www.lambeth.gov.uk/business-rates-services-licensing/business-rates/business-rates-data> | Quarterly | All-properties CSV + reliefs files |
| Islington | <https://www.islington.gov.uk/about-the-council/information-governance/freedom-of-information/publication-scheme/what-we-spend-and-how-we-spend-it/business-rates-data> | Quarterly | Business name (sole traders redacted), address, rateable value, reliefs |

**License:** UK OGL or local-government transparency code.

**Critical coverage gap — Westminster and City of London refuse publication.**
A 2025 Upper Tribunal ruling
(<https://mansfield.bailii.org/uk/cases/UKUT/AAC/2025/54.pdf>) examined
their refusal and noted *"31 out of 33 London boroughs disclose the requested
information."* The two largest office markets by floorspace and RV are
exactly the two that withhold — the coverage gap is structurally aligned
against the most important submarkets (West End = Westminster, City = City
of London).

**Caveats on borough data, all boroughs:**

1. **Empty-property relief ≠ physical vacancy.** The 3-month (or 6-month
   for industrial) exempt period, plus the small-RV indefinite exemption,
   means the relief flag captures administrative status, not physical state.
2. **Cannot distinguish "physically empty" from "empty but sublet."** The
   relief status does not capture informal occupation, licence agreements,
   or subletting arrangements that don't trigger a new rates liability.
3. **"Empty" in business-rates terms = empty of the ratepayer**, not
   necessarily empty of all activity.
4. **Schema varies by borough.** Some publish CSV, some Socrata API, some
   FOI PDFs. Joining across boroughs needs a normalisation layer.
5. **Cadence ranges monthly to 6-monthly.** A consistent quarterly cadence
   is not achievable from these sources alone.

**Verdict:** partial. Potentially canonical for a borough-level
**administrative vacancy indicator** under OGL. **Not** suitable as a
primary vacancy-rate source because of: incomplete coverage (Westminster,
City of London missing), schema variance, relief-vs-vacancy definitional
gap, and spotty cadence.

### Candidate 11: Non-domestic EPC Register

- URL: <https://epc.opendatacommunities.org/> (MHCLG open-data portal)
- **License:** OGL v3.0
- **Cadence:** quarterly / annual bulk; individual certificates searchable.
- **Vacancy proxy logic:** EPCs are triggered on sale / new letting /
  construction. A drop in lodgements could signal reduced market churn.
- **Why it fails:** EPCs are valid 10 years. Existing tenants renewing or
  extending do not trigger new lodgements. Vacant buildings generate no
  EPCs (no trigger event). Cannot distinguish "vacant and silent" from
  "occupied and not transacting."
- **Verdict:** proxy-only. OGL data but not a meaningful vacancy indicator.
  Could support a market-churn / liquidity signal at best.

### Candidate 12: DESNZ subnational energy consumption

- URL: <https://www.gov.uk/government/statistics/subnational-electricity-and-gas-consumption-summary-report-2024>
- ND-NEED: <https://www.gov.uk/government/statistics/non-domestic-national-energy-efficiency-data-framework-nd-need-2024>
- **License:** OGL v3.0
- **Granularity:** LSOA/MSOA for non-domestic meters. ND-NEED links VOA
  rating list → UPRN → meter-point consumption internally; published outputs
  are aggregated only for disclosure control.
- **Vacancy proxy logic:** near-zero consumption at a building could signal
  vacancy. **But** raw meter-point data is **not public** — only accessible
  to DESNZ under legal gateway. Published aggregates are too coarse for
  building-level inference. Multi-tenant buildings aggregate across units.
- **Verdict:** proxy-only. Useful for energy-efficiency analysis; cannot
  serve as a vacancy source.

## Final summary table

| # | Source | License | Granularity | Cadence | Canonical verdict |
|---|---|---|---|---|---|
| 1 | VOA NDR Stock of Properties | OGL v3.0 | LSOA / sector / SCat | Annual | **Partial** — stock baseline only, no vacancy |
| 2 | VOA Business Floorspace | OGL v3.0 | LA / sector / SCat | Annual | **Partial** — floorspace baseline, no vacancy |
| 3 | MHCLG NNDR3 Empty Property Relief | OGL v3.0 | LA, £ only | Annual | **Partial** — administrative signal, no office split |
| 4 | VOA Rating List (raw) | **Restricted** | Hereditament | ~2-monthly + twice weekly | **Rejected** — licence conflict |
| 5 | ONS BRES | OGL / Chancellor's Notice | LSOA / 5-digit SIC | Annual | **Proxy-only** — employment density, not vacancy |
| 6 | GLA Commercial Floorspace | OGL v3.0 | Borough / sector | Annual | **Rejected** — VOA-derived stock |
| 7 | GLA HSDS Vacancy Register | **Subscriber-only** | Property-level | Monthly | **Rejected** — third-party copyrighted (LDC) |
| 8 | Vendor reports (CoStar/JLL/CBRE/Savills/KF/C&W/Montagu Evans/DeVita CME) | Vendor copyright | Vendor submarkets | Quarterly | **Rejected** — `report-derived` only |
| 9 | MHCLG PS1/PS2 | OGL v3.0 | LPA, counts | Quarterly | **Rejected** — pipeline only |
| 10 | ONS Property Unit | N/A | N/A | N/A | **Rejected** — no series held |
| 11 | LA business rates — Barnet / Sutton / Camden / Waltham Forest / Lambeth / Islington | UK OGL | Property-level / relief status | Monthly–quarterly | **Partial** — admin vacancy indicator; coverage gap |
| 12 | LA business rates — Westminster / City of London | **Not published** | N/A | N/A | **Rejected** — withheld; critical coverage gap |
| 13 | Non-domestic EPC Register | OGL v3.0 | Building / EPC rating | Quarterly / annual | **Proxy-only** — churn signal, not vacancy |
| 14 | DESNZ subnational energy / ND-NEED | OGL v3.0 | LSOA / sector / size | Annual | **Proxy-only** — aggregate too coarse |

## Bottom line

> **No single OGL public source measures London office vacancy /
> availability rate at submarket granularity.**

The canonical pipeline has only these layers:

1. **Stock baseline (canonical, OGL)** — VOA Stock of Properties +
   Business Floorspace. Gives the denominator for any rate calculation.
   Annual, down to LSOA by SCat sector.

2. **Administrative vacancy indicator (canonical, partial, OGL)** —
   Aggregated borough business-rates empty-property lists. **Missing
   Westminster and City of London** — structurally aligned against the two
   most important office submarkets. Relief status ≠ physical vacancy.

3. **Vendor-derived availability rates (report-derived, NOT canonical)** —
   Savills / CBRE / CoStar / JLL quarterly reports give the actual vacancy /
   availability % the market uses. Remain in `report-derived` lane with
   citation-only retention, exactly as BNP is handled today.

4. **Proxy signals (supplementary, OGL)** — BRES employment trends, EPC
   lodgement volumes, subnational energy consumption at aggregate level.
   Directionally useful; none definitive.

The `london-office-vacancy` capability stays blocked with explicit reference
to this survey. Even borough business-rates open data, the strongest public
candidate, is structurally incomplete for the Central London office market
because the two dominant billing authorities withhold publication.

## Recommended source/metric classification

```text
office_vacancy_rate              # blocked — no canonical source
  availability_rate              # vendor-only
  physical_vacancy_rate          # no public source
  administrative_empty_relief    # partial — borough business-rates OGL

non_domestic_rating              # distinct family
  stock_property_count           # OGL aggregate, VOA Stock of Properties
  stock_floorspace_m2            # OGL aggregate, VOA Business Floorspace
  rateable_value                 # restricted licence if raw; OGL if aggregate
  empty_property_relief_gbp      # OGL aggregate, NNDR3 return
```

Lane policy:

- **`production_ingestion`** — only OGL sources; VOA Stock of Properties,
  Business Floorspace, MHCLG NNDR3 aggregates, borough business-rates
  open-data lists (where published under UK OGL).
- **`report-derived`** — Savills, CBRE, CoStar, JLL, BNP. Citation-only
  retention; never promoted to canonical.
- **`proxy-indicator`** *(new lane, if added)* — BRES, EPC lodgements,
  DESNZ subnational energy. OGL but directional; must carry explicit
  proxy disclaimer in every read-API response.

## References

- Original research: [[wiki/research/datasource/02-office-stock-availability|辦公室存量與可用樓面 Data Sources]]
- Operational status: [[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status — 2026-08-01]]
- Parallel survey: [[wiki/research/datasource/office-rent-canonical-survey|London Office Rent Canonical-Eligibility Survey — 2026-08-04]]
- Format precedent: [[wiki/research/datasource/planning-data-gov-uk-survey|planning.data.gov.uk Crown Copyright Survey — 2026-08-03]]
- Upper Tribunal ruling on Westminster / City of London business-rates withholding: <https://mansfield.bailii.org/uk/cases/UKUT/AAC/2025/54.pdf>
