---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 市場新聞與重大事件 Data Sources

## 成功：GOV.UK Search API + Content API（免費、免登入）

- 搜尋 API：[GOV.UK Search API 文件](https://docs.publishing.service.gov.uk/repos/search-api/using-the-search-api.html)
- 內容 API：[GOV.UK Content API 文件](https://content-api.publishing.service.gov.uk/)
- 可用方式：先以 Search API 找政策、統計與官方事件，再用結果的 `link` 呼叫 Content API 取得標題、內文、發佈者及更新時間。
- 定位：官方政策／統計／政府事件；不是完整商業地產新聞流。

### 如何取得

1. 搜尋：`GET https://www.gov.uk/api/search.json?q=%22commercial+property%22&count=10&order=-public_timestamp`。
2. 對每個候選結果保留 `title`、`description`、`link`、`public_timestamp`、`format` 及 `organisations`。
3. 人工或 agent 檢查相關性；GOV.UK 查詢不是 commercial-property 專用分類器。
4. 將結果 `link`（例如 `/government/...`）接到 `https://www.gov.uk/api/content`，取得結構化內容。
5. 專案函式：

```python
from nan_fung.datasources.news import fetch_content_item, search_market_news

hits = search_market_news('"commercial property"', count=3)
item = fetch_content_item(hits["records"][0]["url"])
```

## 實際取得的 Example Data

2026-07-31 live 執行 `search_market_news('"commercial property"', count=3)`；以下保留第一筆實際 hit 及原始 envelope 時間。Search API 的 `public_timestamp` 是搜尋排序時間，Search envelope 本身沒有單一 `published_at`：

```json
{
  "category": "market_news_events",
  "source": "GOV.UK Search API",
  "source_url": "https://www.gov.uk/api/search.json?q=%22commercial+property%22&count=3&order=-public_timestamp&fields=title%2Cdescription%2Clink%2Cpublic_timestamp%2Cformat%2Corganisations",
  "retrieved_at": "2026-07-31T14:07:45.572478+00:00",
  "published_at": null,
  "source_updated_at": null,
  "records": [
    {
      "title": "Monthly property transactions completed in the UK with value of £40,000 or above",
      "description": "This series looks at monthly property transactions completed in the UK with value of £40,000 or above.",
      "public_timestamp": "2026-07-31T08:30:05Z",
      "format": "national_statistics",
      "organisations": ["HM Revenue & Customs"],
      "url": "https://www.gov.uk/government/statistics/monthly-property-transactions-completed-in-the-uk-with-value-40000-or-above",
      "content_api_url": "https://www.gov.uk/api/content/government/statistics/monthly-property-transactions-completed-in-the-uk-with-value-40000-or-above"
    }
  ]
}
```

再把同一 hit 傳給 `fetch_content_item()`，取得可分辨首次發佈與最近更新的實際 Content API record；`published_at`／`first_published_at` 是 2013-09-24，`source_updated_at`／`public_updated_at` 是 2026-07-31：

```json
{
  "category": "market_news_events",
  "source": "GOV.UK Content API",
  "source_url": "https://www.gov.uk/government/statistics/monthly-property-transactions-completed-in-the-uk-with-value-40000-or-above",
  "retrieved_at": "2026-07-31T14:07:46.466967+00:00",
  "published_at": "2013-09-24T00:00:00+01:00",
  "source_updated_at": "2026-07-31T09:30:05+01:00",
  "records": [
    {
      "title": "Monthly property transactions completed in the UK with value of £40,000 or above",
      "description": "This series looks at monthly property transactions completed in the UK with value of £40,000 or above.",
      "base_path": "/government/statistics/monthly-property-transactions-completed-in-the-uk-with-value-40000-or-above",
      "document_type": "national_statistics",
      "schema_name": "publication",
      "first_published_at": "2013-09-24T00:00:00+01:00",
      "public_updated_at": "2026-07-31T09:30:05+01:00",
      "organisations": ["HM Revenue & Customs"],
      "content_api_url": "https://www.gov.uk/api/content/government/statistics/monthly-property-transactions-completed-in-the-uk-with-value-40000-or-above"
    }
  ]
}
```

### 更新、地理、授權與限制

- 更新：Search index 會隨 GOV.UK 發佈更新；監察時保存 `base_path`／URL 及 `public_updated_at`，用它們去重及判斷變更。
- 地理：全 GOV.UK；London 必須放入精確 query，並由 agent 檢查內容實際適用地區。
- 授權：GOV.UK 內容通常按 [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) 發佈；個別 attachment／第三方內容要再查頁面聲明。
- 限制：查詢預設是文字檢索；使用 quoted phrase 可降低雜訊，但結果仍要做相關性審核。
- 缺口：不包含完整 brokerage／private-market 報道或全部 leasing transactions。CoStar 等付費／登入來源不列作成功 datasource。
