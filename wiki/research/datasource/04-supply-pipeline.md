---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 供應管線 Data Sources

## 已驗證：Planning London Datahub（PLD）guest API

- 類型：免費、免帳戶的 read-only Elasticsearch API；agent 可直接調用。
- 覆蓋：London planning authorities；資料由申請人及各 LPA 提供。
- 更新：持續同步但沒有保證 SLA；每筆須看 `last_updated`、`valid_date`、`status` 及 `decision`。
- 官方連線說明：[PLD API technical documentation](https://www.london.gov.uk/sites/default/files/planninglondondatahub_api_connection_technical_documentation_v1.pdf)。所有請求必須帶 `X-API-AllowRequest: be2rmRnt&`。

### 取得方法

已知 application id 時直接 GET：

```python
from nan_fung.datasources.planning import fetch_planning_application

item = fetch_planning_application("Tower_Hamlets-PA_26_00372_NC")
```

發現 office 候選項目時使用搜尋端點，再逐筆 GET 完整資料：

```python
from nan_fung.datasources.planning import search_planning_applications

result = search_planning_applications(
    {
        "bool": {
            "must": [{"match_phrase": {"description": "office"}}],
            "filter": [{"range": {"valid_date": {"gte": "01/01/2025"}}}],
        }
    },
    source_fields=[
        "id", "lpa_name", "valid_date", "last_updated", "status", "decision",
        "description", "development_type", "application_details.non_residential_details",
    ],
)
```

等價 HTTP：`POST https://planningdata.london.gov.uk/api-guest/applications/_search`，JSON body 使用 Elasticsearch 7.9 query DSL；單筆為 `GET .../applications/_source/{id}`。

目前函式只回傳 `size` 指定的一頁、沒有 total hits／排序／pagination，因此適合候選項目 triage，**不代表完整 pipeline**。需要全量監察時，必須依 PLD 技術文件加入穩定排序及分頁並另行 live 驗證。

## 實際取得的 Example Data

以下是 2026-07-31 live 調用 `fetch_planning_application("Tower_Hamlets-PA_26_00372_NC")` 的實際 `SourceResult`；`records[0]` 只節錄 qualification 所需的原始欄位，欄名和值未改寫：

```json
{
  "category": "supply_pipeline",
  "source": "Planning London Datahub",
  "source_url": "https://planningdata.london.gov.uk/api-guest/applications/_source/Tower_Hamlets-PA_26_00372_NC",
  "retrieved_at": "2026-07-31T14:08:28.945047+00:00",
  "published_at": null,
  "source_updated_at": "2026-07-30T20:02:54.380Z",
  "records": [
    {
      "id": "Tower_Hamlets-PA_26_00372_NC",
      "lpa_name": "Tower Hamlets",
      "url_planning_app": "https://development.towerhamlets.gov.uk/online-applications/applicationDetails.do?keyVal=DCAPR150148&activeTab=summary",
      "valid_date": "05/03/2026",
      "decision_date": "16/04/2026",
      "status": "Approved",
      "decision": "Approved",
      "description": "Change of use from ancillary management office space to Class E (office) space",
      "development_type": "Other change of use",
      "last_updated": "2026-07-30T20:02:54.380Z",
      "actual_commencement_date": null,
      "actual_completion_date": null,
      "application_details": {
        "total_gia_existing": 169,
        "total_gia_gained": 0,
        "total_gia_lost": 0,
        "phasing_details": [
          {
            "phase_detail": "Entire Development",
            "intended_commencement_date": "04/2026",
            "intended_completion_date": "06/2026"
          }
        ],
        "existing_proposed_floorspace_details": [
          {
            "gia_existing": 169,
            "use_class": "EG1",
            "gia_gained": 0,
            "gia_lost": 0
          }
        ]
      }
    }
  ]
}
```

此 live record 沒有 linked／superseded 欄位，須報作 unknown；`Approved` 及 intended dates 亦不能代替實際 commencement／completion。

## 授權、篩選與缺口

- guest API 免費，但技術文件沒有給資料庫一個明確的獨立開放授權；保留 `Planning London Datahub`、LPA、application id 與原始連結，對外再發布前重新核對條款。
- `description` 提及 office 不等於新增供應。納入 pipeline 前必須核對 use class、GIA gained/lost、development type、phasing、decision，以及來源有提供時的 linked／superseded 狀態；欄位缺失代表 unknown。
- PLD 明示資料未在接收時全面核實；Non-Residential 資料完整度亦可能較低。空值不能當 0，approved 不能當 completed。
- 公開市場報告可補充大型 development／pre-let，但須另標 `report-derived`，不可與 planning facts 混為一項已落成供應。
