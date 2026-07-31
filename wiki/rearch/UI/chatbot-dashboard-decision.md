---
type: decision
status: accepted
date: 2026-07-31
---

# UI Decision: Chatbot Dashboard

## Decision

PoC 將採用簡單的 **chatbot + market dashboard** 介面：使用者可向 Agent 提問；同一頁面提供市場摘要、關鍵指標、新聞／警示及資料來源。

## Rationale

- 測試要求的是可執行、可互動的 Python AI Agent，並沒有規定必須使用特定 UI。
- Chatbot 直接展示 Agent 的互動能力；dashboard 讓業務團隊能快速閱讀市場狀況。
- 這種呈現方式能同時支援 PoC demo 和 3–6 頁的業務簡報。

## MVP Scope

- Chat 問答及附來源的 Agent 回覆。
- 最新市場摘要、重點指標及近期新聞／警示。
- 不包含登入權限、複雜設定、完整通知排程或企業系統整合。

## Success Criteria

使用者能從單一頁面提問並得到具來源的市場分析，同時查看最新的市場重點。
