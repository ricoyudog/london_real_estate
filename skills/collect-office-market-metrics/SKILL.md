---
name: collect-office-market-metrics
description: Route London office rent, vacancy, availability, take-up, deals, stock, and investment questions through the typed agent facade. All headline office-market metrics are blocked; surface the canonical blocked reasons instead of fabricating values.
type: skill
---

# Collect Office Market Metrics

## Start with coverage

Call `describe_market_data` first. The headline London office-market capabilities are product-blocked:

- `london-prime-rent` — prime office rent. `blocked_reason`: "Product coverage is not approved."
- `london-office-vacancy` — office vacancy rate. `blocked_reason`: "Product coverage is not approved."
- `uk-investment-transactions` — investment transactions. `blocked_reason`: "Transaction coverage is not approved."

There is no supported rent, vacancy, availability, take-up, deal, or stock capability in the facade today. Do not claim report-derived, BNP, Rightmove, or any other rent/vacancy numbers as available — they cannot be queried through the facade.

## Handle blocked coverage

When the user asks for any prime rent, vacancy, availability, take-up, Grade A share, major leasing transaction, investment volume, or stock figure, do not estimate, infer, or quote a remembered market value. Submit a `partial` or `unavailable` brief whose `limitations` carry the exact `blocked_reason` text returned by `describe_market_data`. A numeric fact requires a resolved citation from `query_market_data`; without one, the metric cannot be a numeric fact.

After data gathering, hand off to `generate-grounded-market-brief` and complete its required `finalize_market_brief` step.
