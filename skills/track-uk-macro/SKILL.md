---
name: track-uk-macro
description: Retrieve and interpret free official UK interest-rate, monetary-policy, GDP, inflation and labour-market data, including London regional employment indicators. Use when monitoring UK macro conditions, refreshing a London office-market report, comparing UK and London labour signals, or tracing a macro claim to a reproducible BoE, ONS or Nomis endpoint.
---

# Track UK Macro

## Run the official sources

1. Import the required functions from `nan_fung.datasources.macro`.
2. Call `fetch_bank_rate` and `fetch_latest_mpc_decision` for policy conditions.
3. Call `fetch_uk_gdp`, `fetch_uk_inflation`, and `fetch_uk_labour_market` for UK indicators.
4. Call `fetch_london_labour_market` only when a London regional view is needed.
5. Preserve every `SourceResult` field and each record's series or dataset code.

```python
from nan_fung.datasources.macro import (
    fetch_bank_rate,
    fetch_latest_mpc_decision,
    fetch_london_labour_market,
    fetch_uk_gdp,
    fetch_uk_inflation,
    fetch_uk_labour_market,
)
```

## Interpret the result

- State the reference period, publication or update timestamp, geography, unit, and source URL.
- Separate observed facts from agent inference.
- Label ONS headline series as UK and Nomis `E12000007` series as London region.
- Do not present UK data as a London submarket measure.
- Do not present London workforce jobs as people, office-only jobs, or resident employment.
- Mention revisions when comparing values captured on different dates.
- Return a partial result with its limitation when one source fails; do not invent or silently substitute a value.

## Read source details when needed

- Read [利率與貨幣政策](../../wiki/research/datasource/05-interest-rates-monetary-policy.md) for BoE endpoints, samples, licensing, and RSS limitations.
- Read [GDP](../../wiki/research/datasource/06-gdp.md) for `ECYX` and `IHYQ` definitions and revision cautions.
- Read [通脹](../../wiki/research/datasource/07-inflation.md) before comparing CPI, CPIH, and RPI.
- Read [就業市場](../../wiki/research/datasource/08-employment-market.md) for ONS/Nomis query dimensions and London limitations.
