---
type: wiki
updated: 2026-08-01
source: "[[User Requirement]]"
---

# 子市場地理對照 Data Sources

## 已驗證 A：ONS Online Postcode Directory（ArcGIS layer 1）

- 免費 ArcGIS REST query，可把 live postcode 對到官方 LAD／ward／OA／LSOA／MSOA code 及座標。
- 服務：[FeatureServer layer 1](https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Online_ONS_Postcode_Directory_Live/FeatureServer/1)，目前資料為 May 2026；ONSPD 通常每季發布。

```python
from nan_fung.datasources.geography import lookup_postcode

place = lookup_postcode("EC2Y 5AS")
```

真實樣本：`EC2Y 5AS` → `LAD25CD=E09000001`（City of London）、`WD25CD=E05009290`、`LSOA21CD=E01000002`、座標約 51.5177, -0.093894。函式亦讀取 layer metadata；本次 `source_updated_at` 為 2026-06-12T15:10:38.010Z，item 說明的 postcode vintage 為 May 2026。

## 已驗證 B：GLA Town Centre Boundaries（ArcGIS layer 104）

- 免費 polygon API；地理為 London 的 planning town-centre policy boundaries，更新頻率 `ad-hoc`。
- 服務：[FeatureServer layer 104](https://gis.london.gov.uk/arcgis/rest/services/apps/planning_data_map_02/FeatureServer/104)。

```python
from nan_fung.datasources.geography import query_town_centres

canary_wharf = query_town_centres("Canary Wharf", include_geometry=True)
```

真實樣本：Canary Wharf → borough／planning authority `Tower Hamlets`、designation `Town Centres`、classification `Major`；要求 geometry 時回傳 WGS84（WKID 4326）polygon。來源 `notes` 明確表示邊界屬 Planning Authorities，GLA 並未自行 designated 這些 boundaries，輸出時必須保留。

## 實際取得的 Example Data

### ONSPD postcode lookup

以下是 2026-07-31 live 調用 `lookup_postcode("EC2Y 5AS")` 的實際 `SourceResult`：

```json
{
  "category": "postcode_geography",
  "source": "Office for National Statistics Online Postcode Directory",
  "source_url": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Online_ONS_Postcode_Directory_Live/FeatureServer/1/query?where=PCDS%3D%27EC2Y+5AS%27&outFields=PCDS%2CLAD25CD%2CWD25CD%2COA21CD%2CLSOA21CD%2CMSOA21CD%2CLAT%2CLONG&returnGeometry=true&outSR=4326&f=json",
  "retrieved_at": "2026-07-31T14:08:29.374697+00:00",
  "published_at": null,
  "source_updated_at": "2026-06-12T15:10:38.010000+00:00",
  "records": [
    {
      "PCDS": "EC2Y 5AS",
      "LAD25CD": "E09000001",
      "WD25CD": "E05009290",
      "OA21CD": "E00000021",
      "LSOA21CD": "E01000002",
      "MSOA21CD": "E02000001",
      "LAT": 51.5177,
      "LONG": -0.093894,
      "geometry": {
        "x": -0.0938943129204064,
        "y": 51.51770014867171
      },
      "spatial_reference": {
        "wkid": 4326,
        "latestWkid": 4326
      }
    }
  ]
}
```

### GLA Town Centre Boundaries

為保持 example 精簡，以下 live 調用使用 `query_town_centres("Canary Wharf", include_geometry=False)`；將參數改為 `True` 時，原函數會在同一 record 加入 polygon geometry。

```json
{
  "category": "town_centre_geography",
  "source": "Greater London Authority Town Centre Boundaries",
  "source_url": "https://gis.london.gov.uk/arcgis/rest/services/apps/planning_data_map_02/FeatureServer/104/query?where=UPPER%28sitename%29+LIKE+%27%25CANARY+WHARF%25%27&outFields=sitename%2Cborough%2Cplanningauthority%2Cdesignation%2Cclassification%2Clastupdateddate%2Csource%2Cnotes&returnGeometry=false&outSR=4326&resultRecordCount=50&f=json",
  "retrieved_at": "2026-07-31T14:08:30.572788+00:00",
  "published_at": null,
  "source_updated_at": null,
  "records": [
    {
      "sitename": "Canary Wharf",
      "borough": "Tower Hamlets",
      "planningauthority": "Tower Hamlets",
      "designation": "Town Centres",
      "classification": "Major",
      "lastupdateddate": null,
      "source": null,
      "notes": "This data belongs to the Planning Authorities. The GLA has not designated these boundaries.",
      "spatial_reference": {
        "wkid": 4326,
        "latestWkid": 4326
      }
    }
  ]
}
```

## 最新數據取得、授權與限制

1. ONSPD：開啟 [Online ONSPD item](https://www.arcgis.com/home/item.html?id=2ced9a3a2462432a92c31226e3cd3aa5)，核對 `Data updated` 月份及 layer id，再調用 query URL。
2. GLA：開啟 [Town Centre Boundaries dataset](https://data.london.gov.uk/dataset/town-centre-boundaries-e55z7/)，核對 `Last Update` 及 API layer 104，再查詢。
3. ONSPD 包含 Royal Mail、Gridlink、OS 及 ONS 權利，重用前遵守 [ONS Geography licences](https://www.ons.gov.uk/methodology/geography/licences)並保留 attribution。GLA layer 標示 OGL v3，亦須保留其 OS/Crown copyright 說明。
4. **沒有一套免費、官方、通用的 City／West End／Midtown／Fringe 商業辦公子市場 polygon。** ONSPD 是行政地理，GLA town-centre 是 planning policy geography，都不可直接宣稱為 broker submarket。
5. 報告分析應保留每個供應商原有 submarket label；若日後自建映射，另存 version、規則及例外，不把它標成官方邊界。

## Operational persistence status

上述 ONSPD live sample 是 research/legacy lookup evidence。正式
`ons.onspd.postcode` workflow 已實作為 **one-postcode、on-demand** ingestion，
但因 ONS/OS/Royal Mail composite-geodata retention policy，必須由 data-governance
owner 提供明確 retention deadline 後才可取得及保存 raw evidence；它不是
scheduled full-directory collector。詳見
[[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status]]。
