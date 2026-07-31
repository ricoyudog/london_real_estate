---
name: collect-office-market-metrics
description: Collect free London office rents, vacancy, availability, take-up, major deals, stock, and demand proxies. Use for current or historical office-market metric collection, quarterly monitoring, and source-backed market summaries.
---

# Collect Office Market Metrics

## Collect report-derived metrics

1. Open the [BNP research index](https://www.realestate.bnpparibas.co.uk/insights/research).
2. Select the newest `Central London Office Market Update` by quarter and publication date.
3. Record the landing-page URL, publication date, reporting period, and PDF download URL.
4. Extract the PDF with the discovered URL:

```python
from nan_fung.datasources.market import fetch_public_market_report

publication_date = "YYYY-MM-DD"  # Copy from the selected landing page.
report = fetch_public_market_report(url=pdf_url, published_at=publication_date)
```

5. Extract prime rent, vacancy, supply, Grade A share, take-up, and major deals with their page numbers. For deal fields, prefer the `Top Leasing Transactions` table and use narrative text for statuses such as `pre-let`; record any discrepancy.
6. Label every value `report-derived`; retain the provider's unit, definition, submarket label, period, and source URL.

Read [rent](../../wiki/research/datasource/01-office-rent.md), [availability](../../wiki/research/datasource/02-office-stock-availability.md), and [transactions](../../wiki/research/datasource/03-leasing-transactions.md) before publishing results.

## Collect official stock

Open the [VOA collection](https://www.gov.uk/government/collections/non-domestic-rating-stock-of-properties-collection), choose the newest release, and obtain its CSV ZIP. Use the default only while 2026 remains the required release:

```python
from nan_fung.datasources.market import fetch_voa_office_stock

stock = fetch_voa_office_stock("E12000007")
```

Label VOA output `stock`; never interpret hereditament counts as buildings, floor area, availability, or vacancy.

## Collect the free demand proxy

Open Rightmove Commercial Property News, find the newest `Insights Tracker`, and read the office-demand and methodology sections in the normal browser. Record the comparison window and enquiry definition. Label the values `proxy`.

Do not scrape Rightmove, reproduce its charts, or describe enquiries as active requirements. Follow [corporate demand](../../wiki/research/datasource/09-corporate-office-demand.md).

## Report

Keep each provider series separate. Cite every observation and state coverage gaps. Do not list paid products as successful sources.
