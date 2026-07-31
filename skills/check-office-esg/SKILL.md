---
name: check-office-esg
description: Check free official London non-domestic EPC indicators and their office-market limitations. Use when an agent needs an ESG or energy-efficiency signal for London offices, the latest regional EPC rating counts, or a clear assessment of whether property-level EPB data can be fetched anonymously.
---

# Check Office ESG

1. Read [ESG and energy efficiency](../../wiki/research/datasource/11-esg-energy-efficiency.md) for the verified endpoint, schema, sample and caveats.
2. Call `nan_fung.datasources.esg.fetch_non_domestic_epc_ratings("London")` to discover and parse the current live-table attachment.
3. Preserve the quarter, region, attachment URL, source update timestamp and rating counts in the answer; do not relabel an update timestamp as first publication.
4. Label the result `proxy` and state that non-domestic data include offices and other uses.
5. Do not infer whole-stock performance: EPCs only exist when required, buildings can have multiple certificates, and records can be revised or opted out.
6. Treat the record-level EPB API as `pending` until the user supplies a lawful account/token; never claim it is anonymously callable.
