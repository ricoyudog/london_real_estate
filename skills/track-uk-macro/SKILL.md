---
name: track-uk-macro
description: Query UK interest-rate, monetary-policy, and macro data through the typed agent facade, with canonical time anchors, freshness labels, and resolved citations.
type: skill
---

# Track UK Macro

## Start with coverage

Call `describe_market_data` before any query. It is the authority for product coverage, query kinds, limitations, blocked reasons, canonical availability, and allowed refresh profiles. At launch, only `uk.bank-rate-current` is supported. Do not claim that London office rent, vacancy, or transaction data is available. That coverage is blocked.

If the user's time reference would materially change the answer and they have not specified it, ask which time they mean before making any data call. Make zero data calls until they answer.

## Query canonical data

For the supported Bank Rate capability, call `query_market_data` with:

```json
{
  "capability_id": "uk.bank-rate-current",
  "query_kind": "metrics"
}
```

For an explicit historical or as-of request, include the exact RFC3339 UTC `as_of` supplied or confirmed by the user. For an explicit latest request, omit `as_of`.

Treat the returned canonical anchor and each record as the facts available for the answer. Preserve and report, where relevant:

- `anchor_as_of` and `numeric.as_of`
- `source_date` and `retrieved_at`
- `numeric.value`, `numeric.unit`, and `numeric.definition`
- `retrieval_freshness`, `observation_freshness`, `degraded`, `canonical_available`, and warnings

Label stale, degraded, or missing data plainly. Keep last-good canonical data when it is returned, but attach its warning. Never invent a number, date, unit, definition, or missing observation.

## Resolve citations

Take `citation_refs` only from `query_market_data` results. Resolve those exact refs with `get_citation_metadata`, with no more than 20 refs in one call. Never invent a citation or substitute an identifier. A numeric fact needs a resolved citation before it can be presented as a numeric fact.

Keep citation metadata separate from the observation. Report limits and missing publication metadata when returned. If a citation cannot be resolved, do not turn its observation into a numeric fact.

## Refresh carefully

Request refreshes only with `request_data_refresh` and `request_profile: "bank-rate-latest"`. A refresh acknowledgement or status is not market data.

For an accepted or deduplicated request, wait for the returned `poll_after_seconds` before each `get_refresh_status` call. Make at most three status polls in one turn. After terminal refresh status, call `query_market_data` again to obtain canonical data. A terminal status, including success, does not replace that query.

If a refresh is already fresh, still use `query_market_data` for the answer. If approval is required or refresh cannot complete, explain the limitation without filling gaps with model prose or numbers.
