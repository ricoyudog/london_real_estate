---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 混合辦公趨勢 Data Sources

## 成功：ONS Opinions and Lifestyle Survey（免費、免登入）

- 資料頁：[Public opinions and social trends, Great Britain: working arrangements](https://www.ons.gov.uk/peoplepopulationandcommunity/wellbeing/datasets/publicopinionsandsocialtrendsgreatbritainworkingarrangements)
- 已驗證檔案：[3 至 28 June 2026 XLSX](https://www.ons.gov.uk/file?uri=/peoplepopulationandcommunity/wellbeing/datasets/publicopinionsandsocialtrendsgreatbritainworkingarrangements/3to28june2026/workingarrangements3to28june2026.xlsx)
- 可用方式：agent 可直接呼叫專案函式，或到資料頁選最新 edition 的 `xlsx`。
- 定位：`proxy`；這是個人工作安排調查，不是 London 辦公室門禁或實際 occupancy。

### 如何取得

1. 開啟資料頁，讀取最上方最新 edition 的期間、發佈日及 `xlsx` 連結。
2. 下載 XLSX，讀取 `Table_6`；欄 A 是期間，欄 B 至 D 分別是估計百分比、95% 下限及上限。
3. 只保留數值列；`[x]` 是被抑制的估計，不要當成 0。
4. 使用現有已驗證版本時，直接呼叫：

```python
from nan_fung.datasources.hybrid import fetch_hybrid_working

result = fetch_hybrid_working()
previous, latest = result["records"]
print(previous, latest)
```

## 實際取得的 Example Data

2026-07-31 live 執行 `fetch_hybrid_working()` 的實際回傳如下；`retrieved_at` 是本次工具取得時間，`published_at` 是 ONS XLSX 發佈日：

```json
{
  "category": "hybrid_working",
  "source": "ONS Opinions and Lifestyle Survey (OPN)",
  "source_url": "https://www.ons.gov.uk/file?uri=/peoplepopulationandcommunity/wellbeing/datasets/publicopinionsandsocialtrendsgreatbritainworkingarrangements/3to28june2026/workingarrangements3to28june2026.xlsx",
  "retrieved_at": "2026-07-31T14:07:33.527652+00:00",
  "published_at": "2026-07-17",
  "source_updated_at": null,
  "records": [
    {
      "period": "6 to 31 May 2026",
      "geography": "Great Britain",
      "metric": "working adults who both travelled to work and worked from home in the past seven days",
      "estimate_percent": 28,
      "lower_confidence_limit": 26,
      "upper_confidence_limit": 31,
      "indicator_type": "proxy",
      "is_office_occupancy": false
    },
    {
      "period": "3 to 28 June 2026",
      "geography": "Great Britain",
      "metric": "working adults who both travelled to work and worked from home in the past seven days",
      "estimate_percent": 25,
      "lower_confidence_limit": 22,
      "upper_confidence_limit": 28,
      "indicator_type": "proxy",
      "is_office_occupancy": false
    }
  ]
}
```

兩期信賴區間重疊，因此不能只憑點估計下降 3 個百分點便宣稱趨勢已顯著轉弱。

### 更新、地理、授權與限制

- 更新：現行資料約每月發佈；函式固定使用已驗證的 June 2026 檔案。要追最新值，先在資料頁確認新 edition，再更新並重新驗證檔案 URL／sheet schema。
- 地理：Great Britain，調查對象為 16 歲或以上人士；沒有 London、City、West End 或單棟辦公室切分。
- 授權：ONS 網站多數內容按 [Open Government Licence](https://www.ons.gov.uk/help/terms-conditions) 發佈；重用時標示 ONS 來源。
- 限制：抽樣調查有信賴區間、期間長度可能不同，且問題定義在歷史上曾改變。
- 缺口：沒有免費、可匿名取得且已驗證的 London office 實際使用率／門禁資料，因此不能由 25% 推算辦公室 occupancy。
