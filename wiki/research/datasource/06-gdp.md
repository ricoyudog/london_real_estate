---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# GDP Data Sources

## 研究結論

ONS v1 Data API 可直接取得官方時間序列，屬 Tier 1 成功來源；免費、公開且不需 API key。採用：

| Series | 用途 | 頻率 |
| --- | --- | --- |
| `ECYX` / `MGDP` | Gross Value Added，月對月增長，CVM、季節調整 | 月度 |
| `IHYQ` / `QNA` | GDP，季對季增長率，CVM、季節調整 | 季度 |

## 如何取得

ONS v1 使用 evergreen `uri` 查詢 Data endpoint：

```text
GET https://api.beta.ons.gov.uk/v1/data?uri=/economy/grossdomesticproductgdp/timeseries/ecyx/mgdp
GET https://api.beta.ons.gov.uk/v1/data?uri=/economy/grossdomesticproductgdp/timeseries/ihyq/qna
```

回應的 `description` 提供 series metadata；`months` 或 `quarters` 的最後一項是最新 observation。

```python
from nan_fung.datasources.macro import fetch_uk_gdp

result = fetch_uk_gdp()
for observation in result["records"]:
    print(observation["series"], observation["period"], observation["value"])
```

若要自行發現 CDID 的 evergreen URI，可先呼叫：

```text
GET https://api.beta.ons.gov.uk/v1/search?content_type=timeseries&cdids=ECYX,IHYQ
```

## 實際取得的 Example Data

2026-07-31 重新呼叫 `fetch_uk_gdp()`，以下為完整 `SourceResult`：

```json
{
  "category": "gdp",
  "source": "Office for National Statistics",
  "source_url": "https://api.beta.ons.gov.uk/v1/data",
  "retrieved_at": "2026-07-31T14:07:35.320330+00:00",
  "published_at": "2026-07-15T23:00:00.000Z",
  "source_updated_at": "2026-07-15T23:00:00.000Z",
  "records": [
    {
      "series": "ECYX",
      "title": "Gross Value Added - Monthly (period on period growth) :CVM SA",
      "release_date": "2026-07-15T23:00:00.000Z",
      "frequency": "month",
      "period": "2026 MAY",
      "period_basis": null,
      "value": 0.1,
      "unit": "%",
      "updated_at": "2026-07-15T23:00:00.000Z",
      "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Feconomy%2Fgrossdomesticproductgdp%2Ftimeseries%2Fecyx%2Fmgdp"
    },
    {
      "series": "IHYQ",
      "title": "Gross Domestic Product: Quarter on Quarter growth: CVM SA %",
      "release_date": "2026-06-29T23:00:00.000Z",
      "frequency": "quarter",
      "period": "2026 Q1",
      "period_basis": null,
      "value": 0.6,
      "unit": "%",
      "updated_at": "2026-06-29T23:00:00.000Z",
      "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Feconomy%2Fgrossdomesticproductgdp%2Ftimeseries%2Fihyq%2Fqna"
    }
  ]
}
```

## 更新、地理與授權

- `ECYX` 隨 monthly GDP estimate 更新；`IHYQ` 隨 quarterly national accounts 更新。
- 地理範圍是全英國，不能用於比較 City、West End 等 London 子市場。
- [ONS API](https://developer.ons.gov.uk/) 公開且不需 API key；目前標示為 Beta。
- ONS 資料按 [Open Government Licence](https://www.ons.gov.uk/methodology/geography/licences) 免費重用，輸出須標示 Office for National Statistics 為來源。

## 修訂與限制

- GDP 初值會在取得更多資料後修訂；每次報告應保存 `updated_at` 及擷取時間，不應假定舊值不變。
- `ECYX` 是月度 GVA period-on-period growth，不應標成完整季度 GDP；ONS 原始 API 的 `unit` 欄目前為空，專案函式依 series 定義標準化為 `%`。
- `IHYQ` 是 real/CVM、seasonally adjusted 的季對季百分比，不能直接當作 nominal GDP 或年增率。
- v1 API 是 Beta；雖然 series URI 是 evergreen，仍可能有 breaking changes。
