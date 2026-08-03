---
name: assess-office-demand
description: Route London office demand and hybrid-working questions through the typed agent facade. No office-demand capability is exposed today; surface canonical unavailability instead of inferring demand from macro labour data.
type: skill
---

# Assess Office Demand

## Start with coverage

Call `describe_market_data` first. There is no corporate office demand, tenant requirement, or office-occupancy capability in the facade today. Rightmove enquiry proxies, ONS hybrid-working series, and transport-attendance indicators are not exposed as model-callable capabilities.

The model has no Python source access and cannot call `nan_fung.datasources.hybrid` directly. Macro labour series on the facade (employment, unemployment, vacancies, earnings) are UK/London labour-market metrics — they are not office tenant demand and must not be relabelled as such.

## Handle unavailable coverage

When the user asks for tenant demand direction, enquiry signals, hybrid-working prevalence for offices specifically, or any office-occupancy inference, do not derive an office demand figure from labour-market vacancies or present a remembered Rightmove number. Submit a `partial` or `unavailable` brief whose `limitations` name the gap: "Office demand / hybrid-working capability is not approved for the agent facade."

Keep Great Britain and London observations distinct. Do not infer City, West End, Canary Wharf, Midtown, or Fringe demand from national data. A demand inference that lacks a resolved citation from an in-scope capability cannot be presented.

After data gathering, hand off to `generate-grounded-market-brief` and complete its required `finalize_market_brief` step.
