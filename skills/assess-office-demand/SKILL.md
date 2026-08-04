---
name: assess-office-demand
description: Route London office demand and hybrid-working questions through the typed agent facade, keeping Great Britain hybrid-working evidence distinct from London office demand.
type: skill
---

# Assess Office Demand

## Start with coverage

Call `describe_market_data` first. There is no corporate office demand, tenant requirement, or office-occupancy capability in the facade today. Rightmove enquiry proxies and transport-attendance indicators are not exposed as model-callable capabilities.

## Available data

- **`uk.hybrid-working`**: ONS Opinions and Lifestyle Survey hybrid-working measures. Limitations: Great Britain proxy, not London office occupancy or tenant demand.

The model has no Python source access and cannot call `nan_fung.datasources.hybrid` directly. Macro labour series on the facade (employment, unemployment, vacancies, earnings) are UK/London labour-market metrics — they are not office tenant demand and must not be relabelled as such.

## Handle unavailable coverage

When the user asks for tenant demand direction, enquiry signals, hybrid-working prevalence for offices specifically, or any office-occupancy inference, do not derive an office demand figure from labour-market vacancies or present a remembered Rightmove number. Use `uk.hybrid-working` only for its Great Britain survey evidence; otherwise submit a `partial` or `unavailable` brief naming the missing office-demand or occupancy coverage.

Keep Great Britain and London observations distinct. Do not infer City, West End, Canary Wharf, Midtown, or Fringe demand from national data. A demand inference that lacks a resolved citation from an in-scope capability cannot be presented.

After data gathering, hand off to `generate-grounded-market-brief` and complete its required `finalize_market_brief` step.
