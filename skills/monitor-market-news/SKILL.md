---
name: monitor-market-news
description: Route London office market news and event questions through the typed agent facade. Ranked market news is product-blocked; surface the canonical blocked reason instead of fabricating a feed.
type: skill
---

# Monitor Market News

## Start with coverage

Call `describe_market_data` first. The ranked market news capability is product-blocked:

- `uk-ranked-market-news` — `blocked_reason`: "Ranked news coverage is not approved."

There is no supported news search, news content, or news ranking capability in the facade today. Do not call `query_market_data` for news — the capability is `query_disabled: true`.

## Handle blocked coverage

When the user asks for market events, policy updates, leasing announcements, or any ranked/news feed, do not paraphrase a remembered article, quote a publication from training data, or invent a source URL. Submit a `partial` or `unavailable` brief whose `limitations` carry the exact `blocked_reason` text returned by `describe_market_data`. A qualitative fact still requires a resolved citation from `query_market_data`; without one, it cannot become a fact.

After data gathering, hand off to `generate-grounded-market-brief` and complete its required `finalize_market_brief` step.
