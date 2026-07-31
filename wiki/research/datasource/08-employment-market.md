---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 就業市場 Data Sources

## 研究結論

| Data source | 指標 | 結果／取得層級 |
| --- | --- | --- |
| ONS v1 `LMS` | UK employment `LF24`、unemployment `MGSX`、vacancies `AP2Y`、regular pay `KAI9` | 成功；Tier 1 JSON API |
| Nomis `NM_59_1` | London employment／unemployment rates | 成功；Tier 1 JSON API |
| Nomis `NM_130_1` | London total workforce jobs | 成功；Tier 1 JSON API |

全部免費、不需 API key。ONS series 補充全英宏觀背景；Nomis 才提供 London region 指標。

## 如何取得

### UK headline series

```text
GET https://api.beta.ons.gov.uk/v1/data?uri=/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/timeseries/lf24/lms
GET https://api.beta.ons.gov.uk/v1/data?uri=/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms
GET https://api.beta.ons.gov.uk/v1/data?uri=/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/timeseries/ap2y/lms
GET https://api.beta.ons.gov.uk/v1/data?uri=/employmentandlabourmarket/peopleinwork/earningsandworkinghours/timeseries/kai9/lms
```

### London region

使用官方 GSS geography code `E12000007`，不要依賴 Nomis internal geography ID：

```text
GET https://www.nomisweb.co.uk/api/v01/dataset/NM_59_1.data.json?geography=E12000007&time=latest&sex=7&economic_activity=3%2C7&value_type=0&measures=20207
GET https://www.nomisweb.co.uk/api/v01/dataset/NM_130_1.data.json?geography=E12000007&time=latest&industry=37748736&item=1&measures=20100
```

```python
from nan_fung.datasources.macro import (
    fetch_london_labour_market,
    fetch_uk_labour_market,
)

uk = fetch_uk_labour_market()
london = fetch_london_labour_market()
```

## 實際取得的 Example Data

2026-07-31 重新呼叫 `fetch_uk_labour_market()` 及 `fetch_london_labour_market()`。UK 與 London region 是兩個不同地理層級的 `SourceResult`：

```json
{
  "uk": {
    "category": "employment-market",
    "source": "Office for National Statistics",
    "source_url": "https://api.beta.ons.gov.uk/v1/data",
    "retrieved_at": "2026-07-31T14:07:44.915778+00:00",
    "published_at": "2026-07-20T23:00:00.000Z",
    "source_updated_at": "2026-07-20T23:00:00.000Z",
    "records": [
      {
        "series": "LF24",
        "title": "Employment rate (aged 16 to 64, seasonally adjusted): %",
        "release_date": "2026-07-20T23:00:00.000Z",
        "frequency": "month",
        "period": "2026 MAR-MAY",
        "period_basis": "three month average",
        "value": 75.1,
        "unit": "%",
        "updated_at": "2026-07-20T23:00:00.000Z",
        "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Femploymentandlabourmarket%2Fpeopleinwork%2Femploymentandemployeetypes%2Ftimeseries%2Flf24%2Flms"
      },
      {
        "series": "MGSX",
        "title": "Unemployment rate (aged 16 and over, seasonally adjusted): %",
        "release_date": "2026-07-20T23:00:00.000Z",
        "frequency": "month",
        "period": "2026 MAR-MAY",
        "period_basis": "three month average",
        "value": 4.9,
        "unit": "%",
        "updated_at": "2026-07-20T23:00:00.000Z",
        "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Femploymentandlabourmarket%2Fpeoplenotinwork%2Funemployment%2Ftimeseries%2Fmgsx%2Flms"
      },
      {
        "series": "AP2Y",
        "title": "UK Vacancies (thousands) - Total",
        "release_date": "2026-07-20T23:00:00.000Z",
        "frequency": "month",
        "period": "2026 APR-JUN",
        "period_basis": "three month average",
        "value": 712.0,
        "unit": "thousand vacancies",
        "updated_at": "2026-07-20T23:00:00.000Z",
        "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Femploymentandlabourmarket%2Fpeopleinwork%2Femploymentandemployeetypes%2Ftimeseries%2Fap2y%2Flms"
      },
      {
        "series": "KAI9",
        "title": "AWE: Whole Economy Year on Year Three Month Average Growth (%): Seasonally Adjusted Regular Pay Excluding Arrears",
        "release_date": "2026-07-20T23:00:00.000Z",
        "frequency": "month",
        "period": "2026 MAY",
        "period_basis": null,
        "value": 3.4,
        "unit": "%",
        "updated_at": "2026-07-20T23:00:00.000Z",
        "source_url": "https://api.beta.ons.gov.uk/v1/data?uri=%2Femploymentandlabourmarket%2Fpeopleinwork%2Fearningsandworkinghours%2Ftimeseries%2Fkai9%2Flms"
      }
    ]
  },
  "london": {
    "category": "employment-market",
    "source": "Nomis (Office for National Statistics)",
    "source_url": "https://www.nomisweb.co.uk/api/v01/dataset/NM_59_1.data.json?geography=E12000007&time=latest&sex=7&economic_activity=3%2C7&value_type=0&measures=20207",
    "retrieved_at": "2026-07-31T14:07:47.745684+00:00",
    "published_at": null,
    "source_updated_at": null,
    "records": [
      {
        "dataset": "NM_59_1",
        "geography": "London",
        "geography_code": "E12000007",
        "period": "Mar 2026-May 2026",
        "period_code": "2026-05",
        "value": 6.5,
        "status": "Normal Value",
        "source_url": "https://www.nomisweb.co.uk/api/v01/dataset/NM_59_1.data.json?geography=E12000007&time=latest&sex=7&economic_activity=3%2C7&value_type=0&measures=20207",
        "metric": "Total unemployed - aged 16 and over",
        "unit": "percent"
      },
      {
        "dataset": "NM_59_1",
        "geography": "London",
        "geography_code": "E12000007",
        "period": "Mar 2026-May 2026",
        "period_code": "2026-05",
        "value": 73.8,
        "status": "Normal Value",
        "source_url": "https://www.nomisweb.co.uk/api/v01/dataset/NM_59_1.data.json?geography=E12000007&time=latest&sex=7&economic_activity=3%2C7&value_type=0&measures=20207",
        "metric": "Total in employment - aged 16 to 64",
        "unit": "percent"
      },
      {
        "dataset": "NM_130_1",
        "geography": "London",
        "geography_code": "E12000007",
        "period": "March 2026",
        "period_code": "2026-03",
        "value": 6466474,
        "status": "Normal Value",
        "source_url": "https://www.nomisweb.co.uk/api/v01/dataset/NM_130_1.data.json?geography=E12000007&time=latest&industry=37748736&item=1&measures=20100",
        "metric": "total workforce jobs",
        "unit": "jobs"
      }
    ]
  }
}
```

## 更新、地理與授權

- ONS `LMS` 與 Nomis `NM_59_1` 一般隨 monthly labour market release 更新；`NM_130_1` 是季度 workforce jobs。
- ONS 四個 CDID 是 UK；Nomis 查詢固定為 London statistical region `E12000007`，不是 City／West End／Canary Wharf 子市場。
- ONS API 及 [Nomis API](https://www.nomisweb.co.uk/api/v01/about) 均免費且不需 API key。
- ONS／Nomis Crown Copyright data 可依 [Nomis Open Government Licence 說明](https://www.nomisweb.co.uk/home/copyright.asp) 重用，並標示 ONS／Nomis 來源。

## 修訂與限制

- `LF24`、`MGSX` 與 `NM_59_1` 是 Labour Force Survey estimates，受抽樣誤差、方法變更及後續修訂影響；月份 label 多為三個月平均。
- `AP2Y` 是全英三個月平均職位空缺數（單位 thousand vacancies），不是三個月合計，也不是 London 或辦公室行業需求。
- `KAI9` 是 whole-economy regular pay 年增率，不包括 arrears，也不是 London 專屬薪酬。
- `NM_130_1` 是 workplace-based jobs，不是就業人數；一人可有多份工作，亦包括通勤到 London 的工作者。
- `NM_130_1` 目前 seasonally adjusted dataset 的 industry 維度只提供 Total，不能直接分拆 office-using sectors。
