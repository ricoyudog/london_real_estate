---
type: wiki
updated: 2026-08-04
---

# Research Index

> 調研來源、legacy coverage 與 source-policy 邊界；正式 operational 狀態以 Decisions 為準。

## Topics

### Agent Skill and Tool

- [[wiki/research/agent-skill-and-tool/_index|Agent Skill and Tool Research]]

### Datasource

依照 [[wiki/User Requirement|User Requirement]]，目前 **13／13 類均已取得 research-level 可用數據**。
這是研究／legacy `SourceResult` coverage，不等於 production canonical coverage 或
source-policy approval；現時 operational 狀態見
[[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status]]。

接受標準：免費來源只要能取得真實數據即可，包括 direct API、公開報告的 `report-derived` 數據，以及相關的 `proxy` 數據。`proxy`／`report-derived` 均視為已取得，但輸出必須保留類型、原始定義、地理範圍及限制，不可改稱為直接測量值。

> 每一頁現在都有清楚的 `## 實際取得的 Example Data`：API／工具來源列出 live `SourceResult` JSON；報告或網頁來源列出帶頁碼／URL 的 `normalized agent extraction` JSON，而不只寫「驗證成功」。

#### Source surveys (canonical-eligibility deep dives)

- [[wiki/research/datasource/planning-data-gov-uk-survey|planning.data.gov.uk Crown Copyright Survey — 2026-08-03]] — verified OGL v3 source for `london-planning-activity`; documents why `london-project-supply` floorspace is unattainable on public data
- [[wiki/research/datasource/office-rent-canonical-survey|London Office Rent Canonical-Eligibility Survey — 2026-08-04]] — verified no OGL dataset publishes Prime / Grade A / achieved office rent at submarket granularity; `london-office-rent` stays blocked. Covers ONS IPHRP/PIPR, VOA rating list (restricted licence — not OGL), VOA statistical releases, MHCLG historical tables, GLA London Datastore, CoStar/JLL/CBRE/Savills/Knight Frank/Cushman & Wakefield (vendor copyright), MSCI/IPD/Estama (subscription), Eurostat HICP
- [[wiki/research/datasource/office-vacancy-canonical-survey|London Office Vacancy / Availability Canonical-Eligibility Survey — 2026-08-04]] — verified no OGL source measures office vacancy / availability at submarket granularity; `london-office-vacancy` stays blocked. Documents that even the GLA uses CoStar for vacancy analysis, HSDS/LDC is subscriber-only, and Westminster + City of London (the two largest office markets) withhold borough business-rates publication

#### 13 coverage topics

| # | 種類 | 狀態 | 已取得的數據形式 |
| --- | --- | --- | --- |
| 1 | [[wiki/research/datasource/01-office-rent|辦公室租金]] | ✅ 已取得 | 公開報告 `report-derived` |
| 2 | [[wiki/research/datasource/02-office-stock-availability|辦公室存量與可用樓面]] | ✅ 已取得 | VOA API-ready CSV＋公開報告 |
| 3 | [[wiki/research/datasource/03-leasing-transactions|租賃成交]] | ✅ 已取得 | 公開報告的主要交易 |
| 4 | [[wiki/research/datasource/04-supply-pipeline|供應管線]] | ✅ 已取得 | PLD API（legacy）；canonical 現改用 planning.data.gov.uk |
| 5 | [[wiki/research/datasource/05-interest-rates-monetary-policy|利率與貨幣政策]] | ✅ 已取得 | BoE CSV／RSS |
| 6 | [[wiki/research/datasource/06-gdp|GDP]] | ✅ 已取得 | ONS API |
| 7 | [[wiki/research/datasource/07-inflation|通脹]] | ✅ 已取得 | ONS API |
| 8 | [[wiki/research/datasource/08-employment-market|就業市場]] | ✅ 已取得 | ONS／Nomis API |
| 9 | [[wiki/research/datasource/09-corporate-office-demand|企業辦公室需求]] | ✅ 已取得 | Rightmove `proxy` |
| 10 | [[wiki/research/datasource/10-hybrid-working|混合辦公趨勢]] | ✅ 已取得 | ONS survey `proxy` |
| 11 | [[wiki/research/datasource/11-esg-energy-efficiency|ESG 及能源效益]] | ✅ 已取得 | GOV.UK non-domestic EPC `proxy` |
| 12 | [[wiki/research/datasource/12-market-news-events|市場新聞與重大事件]] | ✅ 已取得 | GOV.UK Search／Content API |
| 13 | [[wiki/research/datasource/13-submarket-geography|子市場地理對照]] | ✅ 已取得 | ONSPD／GLA ArcGIS API |

所有已建立的 Python datasource functions 都回傳同一個輕量結構：`category`、`source`、`source_url`、`retrieved_at`、`published_at`、`source_updated_at` 及 `records`。未知日期保留 `null`，不把 observation date 或頁面更新時間冒充首次發布日期。
