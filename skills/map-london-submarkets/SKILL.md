---
name: map-london-submarkets
description: Route London office submarket and postcode geography questions through the typed agent facade. Broker submarkets are blocked and postcode resolution is a hidden approval gate; surface canonical limitations instead of inventing submarket polygons.
type: skill
---

# Map London Submarkets

## Start with coverage

Call `describe_market_data` first. Two geography-related capabilities exist:

- `london-broker-submarkets` — `blocked_reason`: "Broker submarket coverage is not approved." There is no canonical Mayfair / West End / Midtown / Fringe polygon capability.
- `uk.postcode-resolution` — `status: partial`, `query_disabled: true`. Reserved for a hidden Phase 2 approval gate; it is not a model-callable query path today.

The model has no Python source access and no direct ONS / GLA / ArcGIS call. Every geography fact must come through the facade, and neither capability currently returns canonical submarket geography to the model.

## Handle blocked coverage

When the user asks to map a postcode to a submarket, draw a broker submarket polygon, aggregate data by Mayfair / City / Midtown / Fringe, or normalize a free-text location to a canonical office submarket, do not invent a mapping. Submit a `partial` or `unavailable` brief whose `limitations` carry the exact `blocked_reason` text from `london-broker-submarkets` and name the partial status of `uk.postcode-resolution`.

Distinguish ONS administrative geography (borough, LSOA, MSOA) from a broker office submarket — they are not equivalent. Preserve the original submarket label of any cited market-report provider when one exists in canonical data; never relabel an administrative area as a broker submarket.

After data gathering, hand off to `generate-grounded-market-brief` and complete its required `finalize_market_brief` step.
