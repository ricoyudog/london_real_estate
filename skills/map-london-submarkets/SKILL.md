---
name: map-london-submarkets
description: Map London office postcodes to free ONS geography codes and GLA town-centre polygons. Use for location normalization, borough aggregation, spatial joins, and explaining the limits of commercial submarket boundaries.
---

# Map London Submarkets

## Normalize a location

Query the current ONS postcode layer:

```python
from nan_fung.datasources.geography import lookup_postcode

postcode = lookup_postcode("EC2Y 5AS")
```

Keep the returned postcode, LAD, ward, OA/LSOA/MSOA codes, coordinates, source URL, and retrieval time.

## Query a planning town centre

Query GLA layer 104 by name and request geometry only when a spatial join needs it:

```python
from nan_fung.datasources.geography import query_town_centres

centre = query_town_centres("Canary Wharf", include_geometry=True)
```

Retain borough, planning authority, designation, classification, source notes, spatial reference, exact query URL, and polygon provenance. Read [submarket geography](../../wiki/research/datasource/13-submarket-geography.md) for update and licence checks.

## Preserve the distinction

Never equate an ONS administrative area or a GLA planning town centre with a broker office submarket. No universal free official polygon exists for City, West End, Midtown, and Fringe.

Preserve each market-report provider's original submarket label. If a project-specific mapping is required, version its rules and exceptions and label it `custom`, not `official`.

Keep `published_at`, `source_updated_at`, and `retrieved_at` distinct. A null update timestamp means the source did not expose one through this tool; never replace it with retrieval time.
