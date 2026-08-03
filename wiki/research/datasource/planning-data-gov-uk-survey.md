---
type: wiki
updated: 2026-08-03
source: "[[wiki/decisions/london-planning-activity-unlock-2026-08-03|London Planning Activity Unlock — 2026-08-03]]"
tags: [research, datasource, pld, crown-copyright, ogl]
---

# planning.data.gov.uk Crown Copyright Survey — 2026-08-03

## Why this survey

The original [[wiki/research/datasource/04-supply-pipeline|PLD supply-pipeline research]]
pointed at the GLA Planning London Datahub guest Elasticsearch API
(`planninglondondatahub_api_connection_technical_documentation_v1.pdf`,
header `X-API-AllowRequest`, Elastic-style `Tower_Hamlets-PA_26_00372_NC`
IDs). A live probe on 2026-08-03 found that path is no longer usable as
a canonical source for production ingestion. This page records what is
actually available publicly.

## What was probed

### Candidate 1: GLA PLD dashboard on London Datastore

- **URL:** `https://data.london.gov.uk/dataset/pld-non-residential-floorspace-approvals-2k573`
- **Result:** dashboard-only page. **No downloadable assets**.
  CKAN-derived API (`/api/v3/dataset/...`) returns `resources: {}`.
- **Cadence:** page metadata says "Weekly" but the page itself says
  "Last Update: over 3 years ago".
- **License:** page footer says only "© Copyright 2026 Greater London
  Authority" — no explicit OGL statement on this page.
- **Verdict:** rejected as canonical source.

### Candidate 2: planning.data.gov.uk (chosen)

- **URL:** `https://www.planning.data.gov.uk/dataset/planning-application`
- **Asset URLs (all 200, stable):**
  - `https://files.planning.data.gov.uk/dataset/planning-application.csv`
  - `https://files.planning.data.gov.uk/dataset/planning-application.json`
  - `https://files.planning.data.gov.uk/dataset/planning-application.geojson`
- **Maintainer:** Ministry of Housing, Communities and Local Government.
- **License (verified):** OGL v3.0 / © Crown copyright 2026.
  The dataset page embeds `license:
  https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/`
  in JSON-LD and states verbatim: "Licensed under the Open Government
  Licence v.3.0."
- **Cadence:** files updated continuously; published as a national snapshot.

## Schema

CSV header (verified 2026-08-03):

```
dataset, end-date, entity, entry-date, geojson, geometry, name,
organisation-entity, point, prefix, reference, start-date, typology,
address-text, decision-date, description, development-classification,
documentation-url, ground-area, notes, organisation,
planning-application-status, planning-application-type,
planning-decision, planning-decision-type, uprn
```

## Critical source-quality finding

A 118-row sample and a 100,627-row full-CSV inspection (T1 evidence +
T10 live run) both confirmed:

| Field | Populated? |
|---|---|
| `reference` | ✓ |
| `organisation-entity` | ✓ (numeric ID; needs mapping to authority names) |
| `decision-date` | ✓ (ISO date) |
| `description` | ✓ (free text) |
| `entry-date` | ✓ |
| **`ground-area`** | ❌ 100% empty in sample |
| **`planning-application-type`** | ❌ 100% empty in sample |
| **`planning-application-status`** | ❌ 100% empty in sample |
| **`development-classification`** | ❌ 100% empty in sample |
| **`planning-decision`** | ❌ 100% empty in sample |
| **`planning-decision-type`** | ❌ 100% empty in sample |

**Implication:** no floorspace, no use-class classification, no
decision status is exposed by this dataset despite the schema fields
existing. The only honest numeric metric derivable from this public
source is **count of decided applications per organisation-entity per
month** (using `decision-date`).

The dataset itself warns it is "explicitly incomplete and beta" and
that data is applicant-supplied and not quality-checked at receipt.

## 33 London authorities — entity mapping

Sourced from `https://files.planning.data.gov.uk/dataset/local-authority.csv`
(verified 2026-08-03). Embedded as a constant in
`src/nan_fung/ingestion/pld_supply.py:LONDON_AUTHORITY_ENTITY_IDS` and
`:LONDON_AUTHORITY_NAMES`.

| entity | short_name |
| --- | --- |
| 41 | Barking and Dagenham |
| 42 | Brent |
| 43 | Bexley |
| 48 | Barnet |
| 65 | Bromley |
| 90 | Camden |
| 100 | Croydon |
| 115 | Ealing |
| 126 | Enfield |
| 150 | Greenwich (Royal Borough) |
| 162 | Havering |
| 163 | Hackney |
| 167 | Hillingdon |
| 169 | Hammersmith and Fulham |
| 170 | Hounslow |
| 174 | Harrow |
| 175 | Haringey |
| 181 | Islington |
| 182 | Kensington and Chelsea (Royal Borough) |
| 188 | Kingston upon Thames (Royal Borough) |
| 192 | Lambeth |
| 198 | Lewisham |
| 203 | City of London (Corporation) |
| 217 | Merton |
| 246 | Newham |
| 261 | Redbridge |
| 266 | Richmond upon Thames |
| 319 | Sutton |
| 329 | Southwark |
| 350 | Tower Hamlets |
| 366 | Waltham Forest |
| 376 | Wandsworth |
| 387 | Westminster (City) |

Total: 33 (32 London boroughs + City of London Corporation).

## T10 live observation (data-quality caveat)

At the time of the 2026-08-03 live daemon run, the downloaded CSV
(45 MB, 100,627 rows) contained only **4 distinct organisation-entities**
total (1 London: Camden=90, plus 3 non-London: 109, 382, 26). This
confirms the dataset's "explicitly incomplete beta" self-description.
The capability handles this honestly through its `limitations` field
("Source dataset is beta; only Camden has complete coverage as of T10
run").

## Why other candidates were rejected

The wider public-data survey (librarian report, 2026-08-03) covered 12
candidate sources for London-CRE coverage. Highlights of why none of
the others were chosen:

| Candidate | Why rejected |
|---|---|
| ONS API (commercial rent series) | No open London office rent series; ONS uses IPD via secure portal |
| GLA London Datastore (commercial floorspace) | Historical only (2000–2012); no planned update |
| MHCLG quarterly PS1/PS2 planning statistics | Counts only; no office sqft, no London submarket |
| VOA non-domestic rating stock | RV proxy, not market rent; not a supply feed |
| ONS regional GVA (real-estate activities) | Macro context only, not office metric |
| Public investment transactions | No qualifying candidate identified |

For floorspace-based supply (the original `london-project-supply`
intent), **no qualifying public source was identified**. That
capability stays blocked with explicit reference to this survey.

## References

- Decision: [[wiki/decisions/london-planning-activity-unlock-2026-08-03|London Planning Activity Unlock]]
- Original supply-pipeline research (now superseded for canonical use): [[wiki/research/datasource/04-supply-pipeline|supply-pipeline research]]
- T1 live probe evidence: `.omo/evidence/london-supply-unlock/task-1-pld-probe.md` (host-local)
- Implementation commit: `e91620e`
