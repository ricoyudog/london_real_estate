---
type: test-result
status: passed
date: 2026-08-02
source_test_case: "[[wiki/questions/Test_result/Test case 1|Test case 1]]"
runtime_scope: "Pi / GLM-5.2 + typed Python Facade + SQLite canonical store"
---

# TC-01 Agent Runtime Live Test — 2026-08-02

## 結論

**通過（目前 Bank Rate vertical slice 的 runtime gate）。** 真實 GLM-5.2 已能以
Pi session 呼叫受控 tools、完成 host-finalized `market_brief.v1`，並能以中文處理 TC-01。

TC-01 的 London City Prime office rent 不在目前正式 capability coverage 內；正確產品
行為是清楚回覆 coverage unavailable，而不是產生 rent 數字或 citation。本次 live turn
已符合這個行為，且不再因未指定日期被 host 攔截。

這不代表 London office rent 資料已可用；唯一正式 launch capability 仍是
`uk.bank-rate-current`。

## 測試環境

- 工作目錄：`/Users/chunsingyu/softwares/nan_fung/agent-runtime`
- Node：`v22.22.1`；Pi packages：`0.83.0`
- live model：本機 `agent-runtime/.env` 的 GLM-5.2 設定；未記錄或輸出 API key。
- 資料面：typed Python Facade + SQLite canonical store；model 沒有直接 database、shell 或
  filesystem access。

## 修復後實測結果

| 情境 | 實際輸入 | 實際觀察 | 判定 |
|---|---|---|---|
| TC-01 中文原文 | `倫敦金融城本季 Prime office rent 是多少？` | `terminal_state: completed`；沒有 clarification；tool sequence 為 `describe_market_data` → `finalize_market_brief`；artifact 為 `market_brief.v1` / `status: unavailable` / 0 facts / 0 sources。限制清楚說明 London office rent 不在 coverage。 | 通過：不杜撰租金、citation 或數值。 |
| Bank Rate 真實 GLM E2E | `What is the current Bank of England base rate?` | `npm run test:glm` 通過；sequence 為 `describe_market_data` → `query_market_data` → `get_citation_metadata` → `finalize_market_brief`；完成有 citation 的 numeric `market_brief.v1`。 | 通過：真實模型、Pi、Facade、SQLite 與 host finalizer 完整串連。 |
| citation contract regression | seeded Bank Rate query → citation lookup | 正式 `FacadeLauncher` 回傳 `ok`，並解析 1 個 canonical citation；不再依賴測試用的寬鬆 schema workaround。 | 通過：正式 contract 接受安全、有界的巢狀 locator。 |

Bank Rate E2E 的 `5.25%` 是 temporary seeded canonical fixture，用來驗證真實模型與 runtime
鏈路，不是本機資料庫的即時市場數值。

## 本次修復重點

1. 移除未指定日期時的 host-side clarification gate；未指定日期會讓 agent 查詢 latest
   canonical view，保留 `as_of` 和 freshness contract。
2. 對 model-visible tool result 使用 session-local short aliases，例如 `citation_1`；簽名
   handle 僅留在 host 並於 citation/finalizer 呼叫時還原。
3. 修正正式 citation response schema，使其接受 data plane 已限制的巢狀 structural locator，
   並移除測試用的 unrestricted-object workaround。
4. finalization 成功後中止 Pi tool loop；host artifact 優先於後續模型工具嘗試。
5. 對 blocked coverage 增加 `unavailable`、空 facts／inferences 的明確 Skill 規則；將預期的
   draft rejection 轉成安全的 model-visible recovery result。
6. 修正中文 numeric guard，把「一份簡報」等普通語句與「五十英鎊」「百分之五」等數字表達
   區分開來。

## 初測歷史（已修復）

初測曾出現英文 date clarification、citation schema `PROTOCOL_ERROR`、長 signed handle 被模型
轉錄失敗，以及中文量詞被 numeric guard 誤判。上述問題都已有 regression tests 與本次 live
retest 覆蓋。

## 下一步

runtime gate 已可進入 dashboard、Docker 和 UI 實測；UI 必須仍清楚顯示目前只有 Bank Rate
正式 coverage，Prime rent 等區塊為 unavailable，而非顯示虛構 KPI。
