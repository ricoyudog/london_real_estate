---
type: wiki
updated: 2026-07-31
---

# Research Index

> Investigation results from corgi explore sessions.

## Topics

### Datasource

依照 [[User Requirement]]，目前 **13／13 類均已取得可用數據，datasource coverage 已完成**。

接受標準：免費來源只要能取得真實數據即可，包括 direct API、公開報告的 `report-derived` 數據，以及相關的 `proxy` 數據。`proxy`／`report-derived` 均視為已取得，但輸出必須保留類型、原始定義、地理範圍及限制，不可改稱為直接測量值。

> 每一頁現在都有清楚的 `## 實際取得的 Example Data`：API／工具來源列出 live `SourceResult` JSON；報告或網頁來源列出帶頁碼／URL 的 `normalized agent extraction` JSON，而不只寫「驗證成功」。

| # | 種類 | 狀態 | 已取得的數據形式 |
| --- | --- | --- | --- |
| 1 | [[research/datasource/01-office-rent|辦公室租金]] | ✅ 已取得 | 公開報告 `report-derived` |
| 2 | [[research/datasource/02-office-stock-availability|辦公室存量與可用樓面]] | ✅ 已取得 | VOA API-ready CSV＋公開報告 |
| 3 | [[research/datasource/03-leasing-transactions|租賃成交]] | ✅ 已取得 | 公開報告的主要交易 |
| 4 | [[research/datasource/04-supply-pipeline|供應管線]] | ✅ 已取得 | PLD API |
| 5 | [[research/datasource/05-interest-rates-monetary-policy|利率與貨幣政策]] | ✅ 已取得 | BoE CSV／RSS |
| 6 | [[research/datasource/06-gdp|GDP]] | ✅ 已取得 | ONS API |
| 7 | [[research/datasource/07-inflation|通脹]] | ✅ 已取得 | ONS API |
| 8 | [[research/datasource/08-employment-market|就業市場]] | ✅ 已取得 | ONS／Nomis API |
| 9 | [[research/datasource/09-corporate-office-demand|企業辦公室需求]] | ✅ 已取得 | Rightmove `proxy` |
| 10 | [[research/datasource/10-hybrid-working|混合辦公趨勢]] | ✅ 已取得 | ONS survey `proxy` |
| 11 | [[research/datasource/11-esg-energy-efficiency|ESG 及能源效益]] | ✅ 已取得 | GOV.UK non-domestic EPC `proxy` |
| 12 | [[research/datasource/12-market-news-events|市場新聞與重大事件]] | ✅ 已取得 | GOV.UK Search／Content API |
| 13 | [[research/datasource/13-submarket-geography|子市場地理對照]] | ✅ 已取得 | ONSPD／GLA ArcGIS API |

所有已建立的 Python datasource functions 都回傳同一個輕量結構：`category`、`source`、`source_url`、`retrieved_at`、`published_at`、`source_updated_at` 及 `records`。未知日期保留 `null`，不把 observation date 或頁面更新時間冒充首次發布日期。
