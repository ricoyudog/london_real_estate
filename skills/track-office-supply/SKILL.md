---
name: track-office-supply
description: Route London office supply questions through the typed agent facade. Surfaces the in-scope planning-authority activity proxy and the explicitly blocked project-supply (with floorspace) capability.
type: skill
---

# Track Office Supply

Office supply has two product surfaces, both reachable through `describe_market_data`. The model has no Python source access; every fact must come through the facade.

## Start with coverage

Call `describe_market_data` before any query. Two capabilities are relevant:

- `london-planning-activity` — `supported`. Counts planning applications decided per London authority per month. It is activity, not floorspace, and includes all use classes; treat it as a planning proxy, never as delivered office supply.
- `london-project-supply` — `blocked`. Office project supply with floorspace, completion, and change-of-use detail is not approved. The capability carries the canonical `blocked_reason`; quote it verbatim when the user asks for that coverage.

## Query the supported proxy

For City of London authority activity, call `query_market_data` with:

```json
{
  "capability_id": "london-planning-activity",
  "query_kind": "metrics",
  "filters": { "geography_code": "203" }
}
```

Preserve `anchor_as_of`, `numeric.value`, `numeric.unit`, `numeric.definition`, `source_date`, `retrieval_freshness`, `observation_freshness`, `degraded`, and any warnings. Planning-authority granularity only: the 32 London boroughs plus City of London Corporation are supported. City of London authority queries use geography code `203`; named broker submarkets such as Mayfair or the City core remain unavailable.

Resolve `citation_refs` with `get_citation_metadata` (≤20 refs per call). A numeric fact needs a resolved citation before it can be presented.

## Handle blocked coverage

If the user asks for proposed/approved office floorspace, GIA gained, completion dates, refurbishment pipelines, or any specific project supply figure, do not improvise one from planning counts. Use the `blocked_reason` from `london-project-supply` as the limitation text and submit a `partial` or `unavailable` brief. Never derive a supply number from planning-activity counts.

After data gathering, hand off to `generate-grounded-market-brief` and complete its required `finalize_market_brief` step.
