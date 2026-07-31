---
type: wiki
updated: 2026-07-31
source: "[[User Requirement]]"
---

# 企業辦公室需求 Data Sources

## 已驗證：Rightmove Commercial Insights Tracker

- 免費：**是**；公開文章免登入，但沒有公開 API。
- 類型：`proxy`；以用戶向 commercial agents 發出的 listing email enquiries 衡量 demand，不是企業 active requirements 的面積或逐家公司名單。
- 頻率：季度；地理覆蓋全國區域及文章選取的 11 個主要 London boroughs。
- 本次來源：[Q2 2026 Insights Tracker](https://www.rightmove.co.uk/commercial-property/news/q2-2026-insights-tracker//)，2026-07-24 發布；比較期為 2026-04-01 至 2026-06-30 對上一年同期。

### Agent 取得步驟

1. 開啟 Rightmove Commercial Property News，搜尋 `Insights Tracker`。
2. 以文章標題的季度及發布日期選最新一篇，不沿用舊 URL。
3. 讀取 `Key quarterly data`、`Office demand`、`London office demand` 及 `About the Commercial Insights Tracker`。
4. 手動記錄數值、比較期、需求定義、地區及文章 URL；輸出一律標記 `proxy`。
5. 不建立 HTML scraper：Rightmove 在文章內明確禁止 scraping；skill 應以 agent 的正常頁面閱讀和摘要流程運作。

## 實際取得的 Example Data

以下 JSON 是 agent 在正常 browser 閱讀 Rightmove 文章後的正規化 extraction，**不是** API output，亦不是 HTML scraper 結果：

```json
{
  "extraction_type": "normalized agent extraction from webpage",
  "label": "proxy",
  "source": "Rightmove Commercial Insights Tracker",
  "source_url": "https://www.rightmove.co.uk/commercial-property/news/q2-2026-insights-tracker//",
  "article_period": "Q2 2026",
  "published_at": "2026-07-24",
  "retrieved_on": "2026-07-31",
  "comparison_period": "2026-04-01 to 2026-06-30 versus the same period in 2025",
  "geography": "11 selected London boroughs",
  "metric": "office leasing email enquiries to commercial agents via Rightmove",
  "london_year_on_year_change_percent": -11,
  "selected_borough_year_on_year_change_percent": {
    "Lambeth": 1,
    "Kensington & Chelsea": -34,
    "City of London": -30,
    "Hammersmith & Fulham": -26
  }
}
```

## 授權與限制

- Rightmove 內容受版權保護並禁止 scraping；只做人工／agent 閱讀後的有限摘要與引用，不複製整張圖表、批量抓頁或再發布原始內容。
- 這是免費但部分成功的 proxy：listing 組合、平台市佔及 enquiry 重複都會影響結果；不可稱為全倫敦企業需求、active requirements 或實際 take-up。
- 與 BNP take-up／Grade A share 並用時保留不同定義；不要合併為同一指標序列。
