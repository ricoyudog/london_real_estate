---
name: monitor-market-news
description: Monitor free official GOV.UK policy, statistics, news and events relevant to the London office market. Use when an agent needs repeatable market-event discovery, structured GOV.UK article retrieval, or a source-backed update without paid news feeds.
---

# Monitor Market News

1. Read [market news and events](../../wiki/research/datasource/12-market-news-events.md) for endpoints, examples and known coverage gaps.
2. Form a narrow quoted query such as `"commercial property"`, `"London office"` or `"minimum energy efficiency standards"`.
3. Call `nan_fung.datasources.news.search_market_news(query, count=10)` and review every hit for actual market relevance.
4. Call `nan_fung.datasources.news.fetch_content_item(url)` for relevant hits before summarising them.
5. Preserve title, public URL, format, organisation, first-published time and public-updated time; deduplicate by `base_path`.
6. Distinguish official policy or statistical events from commercial-market reporting. State that paid brokerage/news coverage and complete leasing transactions are outside this free feed.
