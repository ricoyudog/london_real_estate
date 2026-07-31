---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 利率與貨幣政策 Data Sources

## 研究結論

| Data source | 結果 | 取得層級 | 費用／認證 |
| --- | --- | --- | --- |
| Bank of England IADB `IUDBEDR` | 成功 | Tier 1：程式直接下載 CSV | 免費；不需 API key |
| Bank of England News RSS | 成功 | Tier 2：agent 直接讀 RSS，再篩選 MPC 項目 | 免費；不需登入 |

## 如何取得

### Official Bank Rate（`IUDBEDR`）

使用 BoE IADB CSV endpoint，改動 `Datefrom`、`Dateto` 即可控制日期範圍：

```text
GET https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01%2FJul%2F2026&Dateto=now&SeriesCodes=IUDBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N
```

CSV 欄位是 `DATE,IUDBEDR`。專案函數已轉換成 ISO 日期及數值：

```python
from nan_fung.datasources.macro import fetch_bank_rate

result = fetch_bank_rate("01/Jul/2026", "now")
latest = result["records"][-1]
```

### 最新 MPC 決議

1. `GET https://www.bankofengland.co.uk/rss/news`。
2. 解析 RSS `<item>`。
3. 選取 `<link>` 包含 `/monetary-policy-summary-and-minutes/` 的第一項。

```python
from nan_fung.datasources.macro import fetch_latest_mpc_decision

result = fetch_latest_mpc_decision()
decision = result["records"][0]
```

## 實際取得的 Example Data

2026-07-31 重新呼叫兩個專案函數。為保持樣本精簡，IADB 的 `records` 只保留回傳陣列的最後一筆 observation；envelope 及 MPC record 均保留可重用欄位：

```json
{
  "bank_rate": {
    "category": "interest-rates-monetary-policy",
    "source": "Bank of England IADB",
    "source_url": "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01%2FJul%2F2026&Dateto=now&SeriesCodes=IUDBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N",
    "retrieved_at": "2026-07-31T14:07:32.243881+00:00",
    "published_at": null,
    "source_updated_at": null,
    "records": [
      {
        "date": "2026-07-30",
        "bank_rate_percent": 3.75,
        "series": "IUDBEDR"
      }
    ]
  },
  "mpc": {
    "category": "interest-rates-monetary-policy",
    "source": "Bank of England News RSS",
    "source_url": "https://www.bankofengland.co.uk/rss/news",
    "retrieved_at": "2026-07-31T14:07:32.880345+00:00",
    "published_at": "2026-07-30T12:00:00+01:00",
    "source_updated_at": null,
    "records": [
      {
        "title": "Bank Rate maintained at 3.75% - July 2026 Monetary Policy Summary and Minutes",
        "url": "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/july-2026",
        "published_at": "2026-07-30T12:00:00+01:00",
        "summary": "The Bank of England’s Monetary Policy Committee is responsible for making decisions about Bank Rate."
      }
    ]
  }
}
```

## 更新、地理與授權

- Bank Rate 序列按英國工作日提供；MPC 通常每年公布八次決議。
- 地理範圍是英國，不是 London 子市場數據。
- IADB 數據可依 [Bank of England legal terms](https://www.bankofengland.co.uk/legal) 按 Open Government Licence 重用並標示來源。
- RSS 可免費存取；RSS 文字屬網站內容，重用時仍須遵守 BoE general copyright terms，agent 應優先保存標題、日期、連結及短摘要。

## 修訂與限制

- `IUDBEDR` 每個工作日重複當日有效利率，不只列出利率變動日。
- 每筆 `date` 是 observation／effective date，不是已證明的發布時間；IADB 沒有在 CSV 提供 publication timestamp，因此結果的 `published_at` 保持 `null`。
- IADB 是舊式 ASP download endpoint，沒有公開 SLA；過量請求可能被限制。
- RSS 是綜合 News feed，必須依 MPC URL path 篩選；網站分類改動時需更新規則。
- RSS 摘要不等於完整 minutes；分析投票或政策理據時須再讀取項目連結。
