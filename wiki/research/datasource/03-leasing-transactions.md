---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 租賃成交 Data Sources

## 已驗證：公開市場報告中的主要交易

- 來源：[BNP Research Insights](https://www.realestate.bnpparibas.co.uk/insights/research)的免費 Central London Office Market Update。
- 類型：`report-derived`；提供季度 take-up、市場活動及主要交易，並非完整租約登記冊。
- 取得最新報告：在 Research Insights 找出日期最新的季度報告 → 開啟報告頁 → 記錄季度／發布日 → 按 `Download` → 將 PDF URL 交給 `fetch_public_market_report` → 由 agent 搜尋 `largest lettings`、`transactions`、`pre-let` 並保留頁碼。

```python
from nan_fung.datasources.market import fetch_public_market_report

report = fetch_public_market_report()
for page in report["records"]:
    if "largest lettings" in page["text"].lower():
        print(page["page"], page["text"])
```

## 實際取得的 Example Data

以下 JSON 是 agent 從 `fetch_public_market_report` 的 live page-text 回傳正規化出的 extraction，**不是** BNP 提供的結構化 API record。交易欄位取自第 10 頁的 `Top Leasing Transactions` 表，`pre-let` 狀態取自第 3 頁敘述：

```json
{
  "extraction_type": "normalized agent extraction from PDF page text",
  "label": "report-derived",
  "source": "BNP Paribas Real Estate Central London Office Market Update",
  "source_url": "https://www.realestate.bnpparibas.co.uk/sites/default/files/2026-05/Q12026CentralLondonMarketUpdate.pdf",
  "report_period": "Q1 2026",
  "published_at": "2026-05-18",
  "retrieved_at": "2026-07-31T14:08:33.226985+00:00",
  "transaction": {
    "tenant": "BP",
    "address": "Ink Building, Timber Square, SE1",
    "floor": "Entire Building",
    "area_sq_ft": 192000,
    "approx_rent_gbp_per_sq_ft": 80.0,
    "term_years": 15,
    "landlord": "Landsec",
    "status": "pre-let",
    "table_page": 10,
    "status_page": 3
  }
}
```

同一份報告內也有口徑差異：BP 面積在部分敘述為約 191,000、交易表為 192,000 sq ft；Gibson Dunn 敘述為 155,000、交易表為 148,000 sq ft。建立結構化記錄時以 `Top Leasing Transactions` 表為主要數值來源、以敘述補充 `pre-let` 等狀態，並保存差異而不是靜默覆蓋。

## 頻率、授權與限制

- 頻率／地理：季度；Central London 及報告內供應商子市場。
- BNP 內容受版權保護；只保存必要交易欄位、短摘要、報告頁碼及 URL，不整份重發。
- 主要交易可接受為本項目的部分成功，但不能聲稱為完整成交量明細；租金、break、incentive 及所有小型交易通常不公開。
- 同一交易可能在多份報告重複出現；去重時使用 tenant + building + approximate area + quarter，並保留 `reported`／`pre-let` 狀態。
