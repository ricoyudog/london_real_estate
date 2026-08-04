---
type: wiki
updated: 2026-08-04
status: accepted
source: "[[wiki/architecture/datasource|Datasource Persistence Architecture: Observation + Evidence Store]]"
tags: [decision, ingestion, capability, pld, london-planning-activity]
---

# London Planning Activity Unlock — 2026-08-03

## Decision

Unlock a new product capability `london-planning-activity` by promoting the
`pld.applications_search` datasource from discovery to production and
surfacing monthly decided-application counts per London planning authority.
The capability is backed by the public Crown-copyright
`planning.data.gov.uk` planning-application dataset (OGL v3.0).

The originally-targeted `london-project-supply` capability (with proposed
floorspace in sqm) remains **blocked**, because the public-data survey
recorded in
[[wiki/research/datasource/planning-data-gov-uk-survey|the source survey]]
found no qualifying public source that supplies floorspace values. The
survey explicitly verified that the `ground-area` and
`planning-application-type` fields in `planning-application.csv` are
unpopulated in source data.

## What changed

### Datasource promotion

| Layer | Before | After |
|---|---|---|
| `pld.applications_search` registry status | `discovery` / `never_canonical` / `restricted` | `production` / `automatic` / `open` |
| Source host allowlist | `planningdata.london.gov.uk` (stale) | `files.planning.data.gov.uk` |
| `pld.api` source binding | `unapproved` / `discovery` | `open_official` / `production` |
| `source_kind` | `structured_api` (25 MB artifact cap) | `file_release` (250 MB cap; the CSV is ~45 MB) |

### New code

- `src/nan_fung/ingestion/pld_supply.py` (~530 LOC) mirrors `bank_rate.py`:
  `SourcePolicy`, sandbox parser aggregating monthly counts per
  `(organisation-entity, period)`, record-key binding, in-memory
  persistence reference adapter, lifecycle, self-check entrypoint.
- `src/nan_fung/workflows.py` gains `OperationalPLDPersistence`,
  `PLDApplicationsSearchLifecycle`, `ingest_planning_applications_artifact`,
  `acquire_live_planning_applications`.
- `src/nan_fung/supervisor.py` dispatches `pld.applications_search` jobs
  through the same capture-before-parse path as Bank Rate.

### New capability + refresh profile

- `london-planning-activity` in `capabilities.v1.json`: `supported`,
  metric `planning_application_count`, decimal string, geography =
  33 London authorities.
- `planning-activity-monthly` in `refresh-profiles.v1.json`.

The old `london-project-supply` capability stays blocked; its
`blocked_reason` now cites the public-data gap analysis recorded in
`.omo/evidence/london-supply-unlock/task-1-pld-probe.md`.

## Honest limitations (recorded in capability manifest)

- Borough-level granularity only; named submarkets (Mayfair, City)
  require polygon overlay which is out of scope.
- Includes all use classes, not office-specific; treat as planning
  activity, not pure office supply.
- Source is applicant-supplied via Crown-copyright planning.data.gov.uk;
  confidence = medium.
- Source dataset is explicitly beta; only Camden has complete coverage
  as of the 2026-08-03 live run.

## Live verification (T10)

`cre daemon once --allow-network` against the real ~45 MB CSV succeeded:

- 44,513,805 bytes downloaded, content-addressed, sandbox-parsed.
- 189 canonical observations ingested and promoted (Camden monthly
  counts 2010-01 to 2025-09).
- No `PARSER_*` errors; sandbox enforced.

The dataset only contained 1 London authority (Camden) at the time of
the run, confirming the source's own "explicitly incomplete beta" warning.
The capability surfaces this honestly through its `limitations` field.

## Tests

- 20 new unit tests in `tests/test_pld_supply.py` cover parser
  aggregation, non-London skip, undecided skip, decimal-string
  normalisation, record-key binding, lifecycle ordering, lane gating.
- `tests/test_ingestion_core.py` policy assertion updated to expect
  `pld.applications_search` in production (was: discovery).
- `agent-runtime/test/multi-turn-probe.test.ts` extended with a 4th
  question; new `seed_pld_activity.py` seeder produces a Camden +
  City of London fixture.

Total: 105 fast-gate + 12 parser-runner + 24 macro + 21 supervisor +
20 PLD tests all pass.

## Why this approach

The [[wiki/research/datasource/planning-data-gov-uk-survey|source survey]]
established that:

1. The original GLA PLD dashboard on London Datastore has **no
   downloadable assets** (resources: {}).
2. `planning.data.gov.uk` exposes real OGL v3 / Crown-copyright CSV /
   JSON / GeoJSON downloads.
3. The schema fields `ground-area`, `planning-application-type`, and
   `development-classification` are **unpopulated** in source data.
4. The only honest numeric metric derivable is monthly decided-application
   counts per London planning authority.

Re-scoping from `london-project-supply` (with floorspace) to
`london-planning-activity` (with counts) was an explicit user decision
after the source-quality findings were surfaced. Renaming the
capability rather than reusing the old name preserves honesty: the
product never claims floorspace coverage it cannot deliver.

## Out of scope

- Polygon overlay for named submarket mapping (Mayfair, City core,
  Midtown, etc.). Geography stays at 33-authority level.
- Unlocking `london-prime-rent`, `london-office-vacancy`, or
  `uk-investment-transactions`. The source survey found no qualifying
  public source for any of these.
- Dashboard UI changes. The dashboard reads through the existing typed
  Facade.
- SQL migration. The existing canonical schema supports new datasources
  without schema change.

## References

- Source survey: [[wiki/research/datasource/planning-data-gov-uk-survey|planning.data.gov.uk Crown copyright survey]]
- Ingestion commit: `e91620e feat(ingestion): unlock london-planning-activity on Crown-copyright planning.data.gov.uk`
- Agent capability commit: `a5c399e feat: unlock london-planning-activity capability and relax streaming numeric guard`
- Live probe evidence: `.omo/evidence/london-supply-unlock/task-1-pld-probe.md` (host-local)
- Operations runbook: [[docs/datasource-operations|Datasource Operations Runbook — PLD section]]
- Acceptance row: TC-04 in [[docs/datasource-acceptance|Datasource Acceptance Matrix]]

## Agent capability delivery (2026-08-04, commit `a5c399e`)

The agent-facing capability was delivered on top of the ingestion foundation:

- **Catalog authority**: `pld.applications_search` promoted from `blocked` to `operational` (`current_vintage_backfill_blocked`); `pld.application` stays blocked.
- **Canonical geography filter**: PLD canonical payload now emits `geography_code` (= organisation entity); the `london-planning-activity` capability manifest permits `geography_code` + source-date range filters. City of London authority `203` + July 2026 window returns count `2`.
- **Host-authoritative facade binding**: `query_market_data` results carry `capability_id`, `datasource_ids`, `normalized_filters`, `result_count` as required contract fields, derived from the manifest — not from model arguments. These are stripped from the model-visible tool output.
- **Trusted finalizer**: the host injects a fixed planning-proxy limitation on planning-backed artifacts; rejects un-negated claims about office supply, floorspace, completions, rent, vacancy, or named-submarket evidence; zero-result planning queries become `unavailable` with a canonical-availability limitation; a zero-result planning query cannot contaminate a later Bank Rate fact in the same turn.
- **Production grant**: `agent-runtime/src/server.ts` authorizes `london-planning-activity` + `planning-activity-monthly` alongside Bank Rate.
- **Streaming NumericGuard redesign**: the guard now filters numeric tokens from streamed prose instead of killing the turn. The draft-level `guardModelText` remains the authoritative numeric boundary. This lets GLM-5.2 narrate naturally while the host retains exclusive ownership of artifact numbers.
- **Deterministic + real tests**: Pi fixture (`08-planning-activity.test.ts`), no-canonical fixture (`09-planning-no-canonical.test.ts`), browser City July + project-supply-unavailable specs, gated real GLM smoke, gated real-browser Playwright E2E.
