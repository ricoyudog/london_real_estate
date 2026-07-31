---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 辦公室租金 Data Sources

## 已驗證：BNP Paribas Real Estate 公開季度報告

- 類型：`report-derived`；免費、免登入的 PDF，不是結構化 API。
- 覆蓋：Central London，並列 City、West End 等供應商自定義子市場。
- 頻率：季度；每季發布日不固定。
- 最新報告入口：[Research Insights](https://www.realestate.bnpparibas.co.uk/insights/research)
- 本次驗證：[Q1 2026 報告頁](https://www.realestate.bnpparibas.co.uk/insights/central-london-office-market-update-q1-2026)及其[公開 PDF](https://www.realestate.bnpparibas.co.uk/sites/default/files/2026-05/Q12026CentralLondonMarketUpdate.pdf)，2026-05-18 發布。

### 如何取得最新數據

1. 開啟 Research Insights，搜尋頁內最新的 `Central London Office Market Update Q…`。
2. 比較報告季度及發布日期，不要假設程式內的預設 PDF 永遠是最新版本。
3. 開啟報告頁，記錄標題、季度、發布日期，再按 `Download` 取得 PDF URL。
4. 將該 URL 傳給函數；agent 從逐頁文字提取租金，保留頁碼與原文上下文。

```python
from nan_fung.datasources.market import fetch_public_market_report

result = fetch_public_market_report()  # 預設為已驗證的 Q1 2026 PDF
# 發現新版時：fetch_public_market_report(url=pdf_url, published_at="YYYY-MM-DD")
page_text = "\n".join(row["text"] for row in result["records"])
```

## 實際取得的 Example Data

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
  "page": 5,
  "submarket": "West End",
  "metric": "prime rent",
  "value": 175,
  "unit": "GBP per sq ft per year",
  "year_on_year_change_percent": 6.1
}
```

這是供應商的 prime-market 指標，不應稱為逐筆 achieved rent。

### 授權與限制

- BNP 內容受版權保護；可在內部研究中下載、摘要及引用，須保留報告連結與發布日期，不應整份重發或建立公開鏡像。
- PDF URL 命名會變；需要 agent 按上述步驟發現最新報告。
- 沒有免費逐筆租約 API，亦沒有統一的 Prime／Grade A 定義。跨供應商比較前必須保留原定義及標記 `report-derived`。
