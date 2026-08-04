---
name: check-office-esg
description: Route London office ESG and energy-efficiency questions through the typed agent facade, including London non-domestic EPC lodgements with explicit scope limitations.
type: skill
---

# Check Office ESG

## Start with coverage

Call `describe_market_data` first. The following capability is available:

## Available data

- **`london.epc-certificates`**: MHCLG Table A non-domestic EPC lodgements for London. Limitations: all non-domestic properties, not offices only; no property-level rating capability.

The model has no Python source access and cannot call `nan_fung.datasources.esg` directly. Every fact must come through the facade.

## Handle unavailable coverage

When the user asks for office-only EPC rating distributions, energy-efficiency indicators beyond the available lodgements, MEES compliance, or property-level ESG signals, do not quote remembered EPC statistics, infer building performance, or fabricate a rating. Submit a `partial` or `unavailable` brief whose `limitations` state that `london.epc-certificates` covers all non-domestic London lodgements, not office-only or property-level ESG data.

If a qualitative ESG observation is supportable only by a resolved citation from `query_market_data` on an in-scope capability, use that path; otherwise the answer is unavailable.

After data gathering, hand off to `generate-grounded-market-brief` and complete its required `finalize_market_brief` step.
