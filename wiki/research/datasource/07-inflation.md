---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 通脹 Data Sources

## 研究結論

ONS v1 Data API 是 Tier 1 成功來源；免費、公開且不需 API key。保留三種 12 個月通脹率，避免把不同指數混為一談：

| Series | 指標 |
| --- | --- |
| `D7G7` / `MM23` | CPI all-items annual rate |
| `L55O` / `MM23` | CPIH all-items annual rate |
| `CZBH` / `MM23` | RPI all-items 12-month percentage change |

## 如何取得

```text
GET https://api.beta.ons.gov.uk/v1/data?uri=/economy/inflationandpriceindices/timeseries/d7g7/mm23
GET https://api.beta.ons.gov.uk/v1/data?uri=/economy/inflationandpriceindices/timeseries/l55o/mm23
GET https://api.beta.ons.gov.uk/v1/data?uri=/economy/inflationandpriceindices/timeseries/czbh/mm23
```

從每個 JSON 回應的 `months` 取最後一項；不要從搜尋結果摘要推算數值。

```python
from nan_fung.datasources.macro import fetch_uk_inflation

result = fetch_uk_inflation()
rates = {row["series"]: row["value"] for row in result["records"]}
```

## 實際取得的 Example Data

2026-07-31 重新呼叫 `fetch_uk_inflation()`，以下為完整 `SourceResult`：

```json
{
  "category": "inflation",
  "source": "Office for National Statistics",
  "source_url": "https://api.beta.ons.gov.uk/v1/data",
  "retrieved_at": "2026-07-31T14:07:39.410713+00:00",
  "published_at": "2026-07-21T23:00:00.000Z",
  "source_updated_at": "2026-07-21T23:00:00.000Z",
  "records": [
    {
      "series": "D7G7",
      "title": "CPI ANNUAL RATE 00: ALL ITEMS 2015=100",
      "release_date": "2026-07-21T23:00:00.000Z",
      "frequency": "month",
      "period": "2026 JUN",
      "period_basis": null,
      "value": 2.6,
      "unit": "%",
      "updated_at": "2026-07-21T23:00:00.000Z",
      "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Feconomy%2Finflationandpriceindices%2Ftimeseries%2Fd7g7%2Fmm23"
    },
    {
      "series": "L55O",
      "title": "CPIH ANNUAL RATE 00: ALL ITEMS 2015=100",
      "release_date": "2026-07-21T23:00:00.000Z",
      "frequency": "month",
      "period": "2026 JUN",
      "period_basis": null,
      "value": 2.8,
      "unit": "%",
      "updated_at": "2026-07-21T23:00:00.000Z",
      "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Feconomy%2Finflationandpriceindices%2Ftimeseries%2Fl55o%2Fmm23"
    },
    {
      "series": "CZBH",
      "title": "RPI All Items: Percentage change over 12 months: Jan 1987=100",
      "release_date": "2026-07-21T23:00:00.000Z",
      "frequency": "month",
      "period": "2026 JUN",
      "period_basis": null,
      "value": 3.0,
      "unit": "%",
      "updated_at": "2026-07-21T23:00:00.000Z",
      "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Feconomy%2Finflationandpriceindices%2Ftimeseries%2Fczbh%2Fmm23"
    }
  ]
}
```

## 更新、地理與授權

- 三個 series 隨 ONS monthly consumer price inflation release 更新。
- 地理範圍是全英國；不能解釋為 London 辦公室營運成本或租金增幅。
- [ONS API](https://developer.ons.gov.uk/) 免費、不需 API key，目前是 Beta。
- ONS 資料按 [Open Government Licence](https://www.ons.gov.uk/methodology/geography/licences) 免費重用，須標示 ONS 來源。

## 修訂與限制

- CPI、CPIH 與 RPI 的涵蓋範圍及方法不同；報告必須保留 series code，不可只寫「inflation」。
- CPIH 包括 owner occupiers' housing costs；CPI 不包括，兩者不可直接互換。
- RPI 是 legacy measure，不應取代 CPI／CPIH 作主要政策指標，但可保留作合約或市場慣例參考。
- 數值可能因修正而更新；保存 `updated_at` 與 `retrieved_at` 以便重現。
