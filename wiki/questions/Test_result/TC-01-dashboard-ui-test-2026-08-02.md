---
type: test-result
status: passed
date: 2026-08-02
source_test_case: "[[wiki/questions/Test_result/Test case 1|Test case 1]]"
runtime_scope: "London Market Desk UI + Pi / GLM-5.2 + typed Python Facade + SQLite canonical store"
---

# TC-01 Dashboard UI Test — 2026-08-02

## 結論

**通過。** 瀏覽器在同源 dashboard 中建立 bearer session、讀取受控的
`dashboard_overview.v1`，並實際送出 TC-01 中文原題：

> 倫敦金融城本季 Prime office rent 是多少？

UI 收到 host-finalized `market_brief.v1` / `status: unavailable`。畫面清楚保留
London Prime rent 為 unavailable，沒有顯示租金數字、facts 或 sources；這符合目前僅有
Bank Rate launch capability 的產品邊界。

## 瀏覽器實測

| 項目 | 實際結果 |
|---|---|
| Overview | Bank Rate 顯示 `5.25 percent`、Bank of England、fresh；四個 London market coverage cards 皆為 unavailable。 |
| TC-01 | final brief 為 unavailable；`facts = 0`；sources drawer 隱藏。 |
| SSE / auth | browser 以帶 `Authorization: Bearer ...` 的 `fetch` stream 讀取 SSE；沒有使用 native `EventSource`。 |
| Browser console | `console_errors = []`。 |
| UI screenshots | [[wiki/questions/Test_result/screenshots/dashboard-overview-bank-rate.png|Overview]]；[[wiki/questions/Test_result/screenshots/dashboard-tc01-unavailable.png|TC-01 conversation]]。 |
| Docker delivery | `nan_fung-market-desk` image 已 build；Compose 以 seeded SQLite bind mount 啟動，第二次 migration 回傳 0 migrations；同一 browser case 在 container port `8787` 通過。 |

![Dashboard overview — temporary seeded Bank Rate](/Users/chunsingyu/softwares/nan_fung/wiki/questions/Test_result/screenshots/dashboard-overview-bank-rate.png)

![TC-01 unavailable result](/Users/chunsingyu/softwares/nan_fung/wiki/questions/Test_result/screenshots/dashboard-tc01-unavailable.png)

## 測試資料與限制

- Bank Rate 的 `5.25` 是隔離 temporary canonical fixture，用於驗證 UI → Facade → SQLite
  鏈路；它不是即時市場數值。
- TC-01 的 unavailable 結果代表目前產品 coverage 正確揭露限制，**不代表** London City
  Prime office rent 資料已經可用。
- Browser run 使用本機 `agent-runtime/.env` 中的 GLM-5.2 設定；沒有記錄或輸出 API key。
- Docker test 的 bind mount 使用 repository 下的已忽略暫存目錄。Colima 對 macOS `/tmp`
  可能提供 VM 自己的空目錄，因此部署時必須選擇 Docker Desktop／Colima 已分享的 host path。
