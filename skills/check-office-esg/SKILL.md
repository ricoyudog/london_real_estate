---
name: check-office-esg
description: Route London office ESG and energy-efficiency questions through the typed agent facade. No office-EPC capability is exposed today; surface canonical unavailability instead of fabricating ratings.
type: skill
---

# Check Office ESG

## Start with coverage

Call `describe_market_data` first. There is no office EPC, ESG, energy efficiency, or property-level rating capability in the facade today. Non-domestic EPC live tables and record-level EPB APIs are not exposed as a model-callable capability.

The model has no Python source access and cannot call `nan_fung.datasources.esg` directly. Every fact must come through the facade.

## Handle unavailable coverage

When the user asks for office EPC rating distributions, energy-efficiency indicators, MEES compliance, or property-level ESG signals, do not quote remembered EPC statistics, infer building performance, or fabricate a rating. Submit an `unavailable` brief with empty `facts` and `inferences` and a `limitations` entry naming the gap: "Office EPC / ESG capability is not approved for the agent facade."

If a qualitative ESG observation is supportable only by a resolved citation from `query_market_data` on an in-scope capability, use that path; otherwise the answer is unavailable.

After data gathering, hand off to `generate-grounded-market-brief` and complete its required `finalize_market_brief` step.
