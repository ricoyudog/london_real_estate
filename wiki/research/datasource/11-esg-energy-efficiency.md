---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# ESG 及能源效益 Data Sources

## 成功：GOV.UK 非住宅 EPC Live Table A（免費、免登入）

- 公開頁：[Live tables on Energy Performance of Buildings Certificates](https://www.gov.uk/government/statistical-data-sets/live-tables-on-energy-performance-of-buildings-certificates)
- 發現 API：[GOV.UK Content API item](https://www.gov.uk/api/content/government/statistical-data-sets/live-tables-on-energy-performance-of-buildings-certificates)
- 可用方式：agent 可經 Content API 自動找出最新 ODS attachment，再讀取 `A_by_Region` 的 London 列。
- 定位：`proxy`；Table A 包含所有非住宅用途，例如 office 及 retail，不是 office-only stock。

### 如何取得

1. `GET` 上述 Content API item；不需要 API key。
2. 在 `details.attachments` 尋找標題 `Table A: Non-domestic Energy Performance Certificates by energy performance asset rating`。
3. 下載該 attachment 的動態 `url`，讀取 ODS worksheet `A_by_Region`。
4. 選 `Region = London` 且 `Quarter` 非空的最後一列；不要使用沒有 quarter 的累計 total 列。
5. 專案函式已完成這個流程：

```python
from nan_fung.datasources.esg import fetch_non_domestic_epc_ratings

result = fetch_non_domestic_epc_ratings("London")
print(result["records"][0])
```

## 實際取得的 Example Data

2026-07-31 live 執行 `fetch_non_domestic_epc_ratings("London")` 的實際回傳如下。`retrieved_at` 是工具取得時間；`source_updated_at` 是 GOV.UK live-table 頁面更新時間；來源沒有提供可安全對應到此季度列的首次發佈時間，所以 `published_at` 保持 `null`：

```json
{
  "category": "esg_energy_efficiency",
  "source": "MHCLG Energy Performance of Buildings live table A",
  "source_url": "https://www.gov.uk/government/statistical-data-sets/live-tables-on-energy-performance-of-buildings-certificates",
  "retrieved_at": "2026-07-31T14:07:44.824891+00:00",
  "published_at": null,
  "source_updated_at": "2026-07-30T09:30:20+01:00",
  "records": [
    {
      "region": "London",
      "quarter": "2026/2",
      "number_lodgements": 3630,
      "total_floor_area_m2": 3102511,
      "rating_a_plus": 13,
      "rating_a": 482,
      "rating_b": 1621,
      "rating_c": 1019,
      "rating_d": 358,
      "rating_e": 119,
      "rating_f": 13,
      "rating_g": 5,
      "not_recorded": 0,
      "indicator_type": "proxy",
      "scope": "all non-domestic properties, not offices only",
      "attachment_url": "https://assets.publishing.service.gov.uk/media/6a69c5bf0825cc51a6c37c9c/A-_Non-Domestic_Properties.ods"
    }
  ]
}
```

### 更新、地理、授權與限制

- 更新：live tables 按季更新；每次都從 Content API 重新發現 attachment，不要永久依賴帶 media id 的 ODS URL。
- 地理：England and Wales；Table A 提供 England regions，包括 London，也提供 local authority sheet。
- 授權：GOV.UK 頁面為 Crown copyright，除另有標示外按 [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) 重用。
- 限制：只涵蓋已登記的 EPC；同一建築可有多張證書，亦有 opt-out／修訂；不能代表完整 building stock。
- 限制：非住宅分類混合 office、retail 等用途，不能將 London 數字直接稱為 London office EPC 分佈。

## Pending：逐棟 EPB API／bulk CSV

- 入口：[Get energy performance of buildings data](https://get-energy-performance-data.communities.gov.uk/)
- 狀態：服務本身免費，但 bulk download 要 GOV.UK One Login，developer API 要 token；不符合「匿名 agent 直接呼叫」，所以不列作本輪成功 API。
- 可建工具：日後由使用者合法建立帳戶、接受資料條款並提供 token 後，再建 credential-aware tool；不得把 token 寫入 wiki 或程式碼。
- 缺口：本輪沒有逐棟 London office 地址／用途篩選，也沒有已驗證的免費 BREEAM building-level feed。
