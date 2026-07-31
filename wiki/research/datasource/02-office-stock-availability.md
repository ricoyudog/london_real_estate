---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 辦公室存量與可用樓面 Data Sources

## 已驗證 A：BNP 公開市場報告（availability／vacancy）

- 類型：`report-derived`；免費 PDF，季度更新。
- 最新發現及下載：開啟 [Research Insights](https://www.realestate.bnpparibas.co.uk/insights/research) → 選日期最新的 Central London Office Market Update → 記錄季度與發布日 → 按 `Download` → 把 PDF URL 傳給 `fetch_public_market_report(url=...)`。
- 本次樣本：[Q1 2026 PDF](https://www.realestate.bnpparibas.co.uk/sites/default/files/2026-05/Q12026CentralLondonMarketUpdate.pdf)列出 Central London supply **22.8m sq ft**、vacancy **8.4%**；Grade B 佔可用供應 73%；West End vacancy 6.8%，City 9.8%。

```python
from nan_fung.datasources.market import fetch_public_market_report

report = fetch_public_market_report()
```

## 已驗證 B：VOA NDR Stock of Properties（stock 基準）

- 類型：英國官方 CSV ZIP，可直接由程式下載；免費、免 key。
- 覆蓋：England and Wales，可按 region／local authority 等官方區域碼查詢。
- 頻率：年度；本次最新為 2026-05-14 發布、截至 2026-03-31 的資料。
- 發現入口：[VOA Stock of Properties collection](https://www.gov.uk/government/collections/non-domestic-rating-stock-of-properties-collection)。選最新年份後下載 `NDR Stock of Properties Tables, YYYY (CSV)`；程式目前已驗證[2026 ZIP](https://assets.publishing.service.gov.uk/media/69f9bdf9a96f4d06cda76fbf/ndr_stock_of_properties_2026.zip)內的 `table_SOP5_1.csv` 及 `table_SOP5_2.csv`。

```python
from nan_fung.datasources.market import fetch_voa_office_stock

london = fetch_voa_office_stock("E12000007")
```

City of London 等 local-authority code 亦可獨立查詢。

## 實際取得的 Example Data

### BNP availability／vacancy

以下 JSON 是 agent 從 `fetch_public_market_report` 的 live page-text 回傳正規化出的 extraction，**不是** BNP 提供的結構化 API record：

```json
{
  "extraction_type": "normalized agent extraction from PDF page text",
  "label": "report-derived",
  "source": "BNP Paribas Real Estate Central London Office Market Update",
  "source_url": "https://www.realestate.bnpparibas.co.uk/sites/default/files/2026-05/Q12026CentralLondonMarketUpdate.pdf",
  "report_period": "Q1 2026",
  "published_at": "2026-05-18",
  "retrieved_at": "2026-07-31T14:08:33.226985+00:00",
  "page": 4,
  "geography": "Central London",
  "supply_million_sq_ft": 22.8,
  "vacancy_rate_percent": 8.4,
  "grade_b_share_of_supply_percent": 73,
  "west_end_vacancy_rate_percent": 6.8,
  "city_vacancy_rate_percent": 9.8
}
```

### VOA stock baseline

以下是 2026-07-31 live 調用 `fetch_voa_office_stock("E12000007")` 的實際 `SourceResult`：

```json
{
  "category": "office_stock",
  "source": "Valuation Office Agency NDR Stock of Properties",
  "source_url": "https://assets.publishing.service.gov.uk/media/69f9bdf9a96f4d06cda76fbf/ndr_stock_of_properties_2026.zip",
  "retrieved_at": "2026-07-31T14:08:30.519110+00:00",
  "published_at": "2026-05-14",
  "source_updated_at": null,
  "records": [
    {
      "geography": "REGL",
      "area_code": "E12000007",
      "area_name": "London",
      "year": 2026,
      "office_property_count": 103400,
      "total_rateable_value_gbp_thousands": 9264908
    }
  ]
}
```

## 授權、口徑與缺口

- VOA 公開統計按 GOV.UK 的 [Open Government Licence 3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)重用，須標示來源與版本。
- VOA 的單位是 rating-list `hereditaments`，不是樓宇數；此 ZIP 是 property count／rateable value，**不是 vacancy 或 available floor area**。
- BNP 的 availability／vacancy 是供應商口徑、受版權保護的 `report-derived` 指標；只摘要引用，不重發整份報告。
- 因此兩個來源只能互補：VOA 作年度存量基準，BNP 作季度市場可用量；不可用 VOA count 推算 vacancy。
