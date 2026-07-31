---
name: track-office-supply
description: Search free Planning London Datahub records and VOA stock data for London office supply signals. Use for new-build, refurbishment, change-of-use, phasing, approval, completion, and annual stock research.
---

# Track Office Supply

## Find planning candidates

Search PLD with narrow date and office terms, requesting only fields needed for triage:

```python
from nan_fung.datasources.planning import search_planning_applications

candidates = search_planning_applications(
    {
        "bool": {
            "must": [{"match_phrase": {"description": "office"}}],
            "filter": [{"range": {"valid_date": {"gte": "01/01/2025"}}}],
        }
    },
    source_fields=[
        "id", "lpa_name", "valid_date", "last_updated", "status", "decision",
        "description", "development_type", "application_details.non_residential_details",
    ],
)
```

Treat the returned page as candidate triage, not an exhaustive pipeline: the helper does not expose total hits, stable sorting, or pagination. Fetch every retained candidate by id:

```python
from nan_fung.datasources.planning import fetch_planning_application

application = fetch_planning_application(candidate_id)
```

## Qualify supply

Check use class, GIA gained and lost, development type, phasing, commencement/completion dates, decision, and linked/superseded fields when the source supplies them. Treat missing or null as unknown, not zero or false. Treat `Approved` as permission, not delivery. Reject address-numbering and incidental-office matches that do not alter supply.

Use VOA office-property counts only as an annual stock baseline. Keep report-derived development and pre-let claims separate from planning facts.

Read [supply pipeline](../../wiki/research/datasource/04-supply-pipeline.md) and [stock/availability](../../wiki/research/datasource/02-office-stock-availability.md) for endpoint, attribution, and data-quality limits.

## Report

Return application id, LPA, dates, status, description, GIA change, phase, source URL, and `last_updated`. State confidence and missing fields. Do not list paid datasets as successful sources.
