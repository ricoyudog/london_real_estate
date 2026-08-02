---
type: test-result
status: passed
date: 2026-08-02
source_test_case: "[tests/Test case.md](../../../tests/Test%20case.md)"
runtime_scope: "Pi createAgentSession / GLM-5.2 + typed Python Facade + SQLite canonical store"
---

# TC-01 Agent Runtime Live Test — 2026-08-02

## 結論

**通過（Bank Rate vertical slice）。** `npm run test:glm` 與 fresh Docker browser
都使用 private `agent-runtime/.env` 的 `glm/GLM-5.2`，沒有 `modelsOverride` 或 fake
session factory。模型經 Pi `createAgentSession` 選擇固定 typed tools，最後只由 host
finalizer 產生 `market_brief.v1`。

唯一正式 numeric capability 仍是 `uk.bank-rate-current`。TC-01 Prime rent、London
office vacancy 與其他未核准 coverage 必須回覆 unavailable，不得生成數字或 citation。

## Live model evidence

| Run | Observation | Result |
|---|---|---|
| CLI real-model gate | `npm run test:glm`；private `.env`，沒有 fake runtime。 | `terminal_state: completed`；Bank Rate numeric artifact 有 canonical source。模型在 host 允許的兩次 finalizer budget 內重試一次，仍保留 `query_market_data → get_citation_metadata → finalize_market_brief` lineage。 |
| Docker Bank Rate | turn `UhnCoGfGeJLIvMa6w5idvw` | `pi-agent-session` / `glm/GLM-5.2`；完整 `describe → query → citation → finalize`；`duration_ms: 19764.027`。 |
| Docker overview | turn `Zv22sIqZFGSuto7pYpCkxg` | 完整 query/citation/finalizer；host artifact 為 `Partial`，清楚列出 office-specific gaps。 |
| Docker unsupported vacancy | turn `stk_1xOQF876CMPqA2p1Tw` | `describe → finalize`；artifact `Unavailable`，0 vacancy numeric facts。 |
| Docker cancel | turn `j2oYm8y3wDU_I5tqvmS-Dg` | `terminal_state: cancelled`；`duration_ms: 282.173`；沒有延遲 45 秒或錯標 `RUNTIME_UNAVAILABLE`。 |

Browser turn ID、SSE runtime badge 與 backend `pi_turn_trace.v1` 已逐輪對帳。Trace 只含
turn ID、runtime identity、model、tool sequence、terminal state、duration 與安全 reason
code，不含 prompt、bearer 或 API key。

## Correctness gates

- stalled prompt 在 45 秒 deadline 後 fire-and-forget abort，不等待 Pi abort settle；
  `TurnContext` 一定清除。
- active cancel 會立即 race prompt、呼叫 Pi abort、輸出 cancelled outcome，且不把已終止
  SSE 的舊 tool events 再投影成第二個 terminal。
- `get_refresh_status` 以呼叫參數的 `job_ref` 做 cadence/re-query correlation。
- finalizer 只從 host ledger 產生 numeric facts、sources、freshness warnings、confidence、
  publication state 與 lineage；stale/degraded fact 不得顯示為無警告的 complete。
- `public_url` 只接受安全 HTTP(S) links；blocked coverage 使用空 facts/sources 的
  unavailable shape。

## Security boundary

Pi runtime 沒有 database、shell、filesystem、collector、raw evidence 或 approval UI。
它只看 host-preloaded Skills 與固定 typed tool surface；signed canonical handles 在 host
內還原。Demo session 沒有 refresh profile，因此 Linux container 不會留下無 daemon 可處理
的 queued refresh job。

完整 UI、Docker 與 screenshot evidence 見
[[wiki/questions/Test_result/TC-01-dashboard-ui-test-2026-08-02|TC-01 Dashboard UI Test — 2026-08-02]]。
