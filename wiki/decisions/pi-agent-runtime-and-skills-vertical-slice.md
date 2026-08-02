---
type: wiki
updated: 2026-08-02
status: accepted
implementation_status: planned
phase_1_prerequisite: verified
source: "[[wiki/decisions/agent-tool-facade-foundation|Agent Tool Facade Foundation]]"
tags: [agent, runtime, pi, skills, mvp, decision]
---

# Pi Agent Runtime and Skills Vertical Slice

> **Implementation state: planned, ready for Phase 2 implementation.** Phase 1 的
> Agent Tool Facade exit gate 已驗證；本文件是 Phase 2 唯一可執行的 decision
> plan。本次不建立 `agent-runtime/` 或開始 Node/Pi 實作。

## Decision

建立一個獨立、最小的 Node/TypeScript `agent-runtime/` package，使用 Pi 原生
`AgentSession` 將一個 Market Analyst 接到 Phase 1 已完成的五個 typed Facade
tools。第一個產品 vertical slice 只支援 **UK Bank Rate**；它可以產生帶
canonical citation 的 grounded market brief，但不得把 macro proxy 說成 London
office rent、vacancy 或交易資料。

Python 仍是唯一 data、workflow、persistence 及 canonical writer plane。Node 只負責
Pi interaction runtime、受控 Facade launcher、turn ledger、in-memory product transport
及 approval continuation。Model 不取得 filesystem、shell、network、database、lane、
promotion 或 canonical writer capability。

ONSPD approval 是 **mandatory offline Phase 2c gate**，以 hidden/test-only policy
驗證；它不加入 production launch allowlist。Production policy 只允許
`uk.bank-rate-current` 與 `bank-rate-latest`。

本 Decision 不建立 competition profile、generic policy engine、task manager、
recursive child-agent system、RAG、datasource migration、dashboard、production auth 或
generic Agent artifact store。

## Non-negotiable delivery gates

Phase 2 必須依序完成以下 gates；任何較後 stage 都不能取代較前 stage 的驗收。

| Stage | Required delivery | Completion rule |
|---|---|---|
| Phase 2a — Runtime Core | Pi boot、Node→Python launcher、五個 Facade tools、finalizer、host-preloaded Skills、turn ledger、direct turn runner、deterministic faux tests | 在沒有 HTTP/SSE 的情況下，direct runner 可完成七個 prompt fixtures 的 core 行為 |
| Phase 2b — Product Transport | In-memory session registry、versioned HTTP、authenticated SSE、replay/recovery、cancel | 受 session capability 保護的 HTTP/SSE 可恢復每個 logical turn，並維持 event/terminal invariants |
| Phase 2c — Approval | Hidden ONSPD approve/deny、expiry、replay、scope、same-session continuation integration | test-only ONSPD policy 完整通過；production policy 仍只開放 Bank Rate |
| Phase 2 complete | 2a、2b、2c 全部通過 | 不以 real-model smoke、dashboard 或 ONSPD production rollout 代替其中任何 gate |

## Runtime baseline

### Package boundary and lockfile

新增一個獨立 `agent-runtime/` package；**不**建立 root npm workspace，也不改變
Python package 的 dependency boundary。其 `package.json` 固定以下 exact versions：

```json
{
  "engines": { "node": ">=22.19.0" },
  "dependencies": {
    "@earendil-works/pi-coding-agent": "0.83.0",
    "@earendil-works/pi-ai": "0.83.0",
    "typebox": "1.3.7",
    "ajv": "8.20.0"
  },
  "devDependencies": {
    "typescript": "5.9.3",
    "@types/node": "22.20.0"
  }
}
```

只使用 Node built-in `node:http` 與 `node:test`；提交 `package-lock.json`，CI 和
clean install 只執行 `npm ci`。不可由 `npm install`、浮動 semver、GitHub `main` 或
root workspace 隱式改變 resolved graph。

預定最小目錄責任如下，供實作者分工而非額外架構抽象：

```text
agent-runtime/
  package.json / package-lock.json / tsconfig.json
  src/
    boot.ts                 Pi boot、resource isolation、Skill verification
    facade-launcher.ts      fixed-child launcher and Phase 1 contract validation
    tools.ts                five Pi adapters and finalize_market_brief
    turn-runner.ts          direct runner, budgets, polling and ledger
    finalizer.ts            draft validation and host hydration
    sessions.ts             in-memory registry and no-reuse scopes
    http.ts / sse.ts        versioned transport and event replay
    approval.ts             Phase 2c continuation only
  test/                     fauxProvider fixtures and deterministic gates
```

### Pi boot and model policy

Pi 使用 programmatic `createAgentSession()`，不是 Pi RPC subprocess。啟動時必須：

- 要求明確 `PI_MODEL=provider/model`；未設定、model 不存在或未授權一律 fail
  startup，不 fallback、不選第一個 model、不 provider cycle。
- `ModelRuntime` 不得做 network catalog refresh；真實 provider inference 仍由明確
  deployment/authentication 設定控制。
- 使用 `SettingsManager.inMemory()`、`SessionManager.inMemory()`，以及每個 runtime
  私有、空的 `cwd` 和 `agentDir`；它們不是 repository root。
- 以 `DefaultResourceLoader` 關閉 extensions、Skill discovery、prompt templates、
  themes 及 context files。啟動後發現任何 extension、discovered Skill、template、
  theme 或 context file 均 fail closed。
- `systemPromptOverride` 只使用 version-controlled Market Analyst prompt；不載入
  AGENTS、CLAUDE、project/global setting 或 working-tree instructions。

Host 在 boot 時直接讀取並驗證只有兩個檔案：

```text
skills/track-uk-macro/SKILL.md
skills/generate-grounded-market-brief/SKILL.md
```

每個檔案必須是 regular file、非 symlink、至多 64 KiB、SHA-256 與
version-controlled manifest 一致。額外 Skill、hash mismatch、symlink、遺失檔案或
超限一律令 startup fail。驗證後將兩份**完整內容**預載到 version-controlled system
prompt；沒有 progressive Skill loading、`read` adapter 或 Skill router，模型不能從
filesystem 重新讀取或替換它們。

Active Pi tool names 必須精確等於：

```text
describe_market_data
query_market_data
get_citation_metadata
request_data_refresh
get_refresh_status
finalize_market_brief
```

六個 tool 都設定 `executionMode: "sequential"`。`approve_refresh` 是 host-only，
不得註冊。`read`、`grep`、`find`、`ls`、`bash`、`edit`、`write`、extension tools
及任何 implicit Pi built-in 均不得出現；啟動 assertion 比對 exact tool-name set。

### Facade launcher and child-process boundary

每次 Pi data-tool call 都由 Node 以 Phase 1 binary 呼叫一個 child process：

- binary path 是 resolved absolute allowlisted path；selector 是固定 argv element；
  `shell: false`；child `cwd` 是私有空目錄；environment 是最小 allowlist。
- 每次 runtime boot 產生一把 32-byte handle key。每次 child invocation 只經 FD 3
  傳入該 key，child 讀後立刻 close；key 不得出現在 argv、environment、stdin、
  stdout、SSE、URL、log 或磁碟。
- 保持 Phase 1 protocol bounds：stdin 64 KiB、stdout 256 KiB、stderr 64 KiB、
  child timeout 10 秒。timeout、cancel 或任何 output overflow 時，先向整個 child
  process group 發 TERM；1 秒後仍未停止才 KILL。
- Node 以 Ajv 載入 Phase 1 的 Draft 2020-12 catalog、generic request/result schema；
  每次驗證 single JSON、request ID echo、argv selector 的 arguments/success-data
  contract、`refresh_request_id` policy，以及 process exit code 與 result error 的
  一致性。任何不一致只可成為 safe typed tool failure，不能進 model context。
- 固定 dependency set 不包含 `ajv-formats`。Ajv 2020 strict setup 必須把 `date` 與
  `date-time` 登記為 annotation-only format names（`true`），而不是關閉 strict mode
  或暗中新增 plugin；Phase 1 schema 的 explicit patterns 和 Python Facade 的 strict
  RFC3339/calendar parser 仍是 timestamp acceptance authority。

Read selector 的 launcher/process test 必須證明它沒有建立 Python
`OperationalStore`、`OperationalRefreshBackend` 或 `RefreshBroker`；這個證據由
Phase 1 selector constructor isolation 延續到 Node integration。

### Turn runner, budget and time rules

每個 logical turn 由 host-owned direct runner 驅動；model 不可自行 reset 計數器或
deadline：

| Boundary | Fixed limit |
|---|---:|
| Facade calls per turn | 8 |
| `get_refresh_status` polls per turn | 3 |
| `finalize_market_brief` calls per turn | 2 |
| query/citation items per call | 20 |
| cumulative records / citations | 40 / 40 |
| cumulative model-visible tool JSON | 128 KiB |
| refresh wait | 15 seconds |
| logical-turn deadline | 45 seconds |
| rendered output | 4096 tokens |

Poll cadence 使用 monotonic clock，並尊重 Phase 1 `poll_after_seconds`；不得以
wall clock、model text、retry 或 continuation 繞過。Refresh terminal 後必須重新
`query_market_data` canonical，不能把 request acknowledgement、job payload 或
terminal state 當數據。Cancel 停止 Pi 和 active child，但不 rollback 已 durable
queued refresh。

時間政策：明確 historical/as-of 只查該 canonical anchor；明確 `latest` 查 canonical
latest；若時間會實質改變答案但使用者未指定，先 clarification 且零 data calls。
relative-time fixture 必須固定 `Europe/London` 與 absolute start/end boundaries，
而不是在 test execution 時取本機日期。

## Structured brief and finalizer

`finalize_market_brief` 是 runtime-only validation tool，既不經 Python Facade，
也不寫 datasource store。Model 只可提交 bounded `market_brief_draft.v1`：

```text
title
status: complete | partial | unavailable
facts[] (max 12)
  claim_id
  kind: numeric | qualitative
  confidence: high | medium | low
  text: qualitative only
  supporting_citation_refs[]: qualitative only
  numeric_citation_ref: numeric only
inferences[] (max 8)
  claim_id
  text
  confidence: high | medium | low
  supporting_fact_ids[]
  caveat
limitations[]
```

Numeric fact 不得提交 value、unit、definition、date、source metadata 或任何 numeric
display text；它只能提交 `numeric_citation_ref`。Inference 只能引用已存在的
fact IDs，且必須有 caveat。Duplicate claim ID、unknown ID、過量 fact/inference 或
schema escape 皆 deterministic reject。

Host turn ledger 為每次成功的 query/citation 保存 scope-bound resolved state：
canonical anchor/run、observation/evidence identity、numeric projection、freshness 和
safe citation metadata。Finalizer 必須拒絕：unknown、cross-session、cross-scope、
cross-anchor、duplicate、未 resolve 的 refs，或 citation/observation/evidence
lineage 不一致的草稿。

所有 model-authored title、qualitative fact、inference、caveat、limitation、
clarification 和 coverage-unavailable text 都先 buffer；numeric guard 拒絕 Unicode
numbers、percent/currency token，以及常見 number-word bypass。Raw model numeric prose
永不直接 stream 至產品。

成功時 host 由 ledger hydration/render 產生 `market_brief.v1`：numeric fields、
`as_of`、sources、lineage、freshness warnings 和 display text 全部由 host 產生；
model 不得覆寫。`published_at` 可為 `null`，但 host 必須在 artifact 及 UI warning
中明示。Datasource confidence、fact confidence 和 inference confidence 是三個不同
欄位，不能合併或互相推導。

## Phase 2b product transport

### Session registry and capability

registry 是 process-lifetime in-memory state，不宣稱 durable session service：

- `POST /v1/sessions` 建立一個 32-byte client-held bearer capability 和一個新的
  Phase 1 capability scope；bearer 只在此 response 回傳一次。
- registry 之後只保存 bearer hash，使用 constant-time comparison。capability 不進
  model prompt、SSE payload、URL、query string 或 logs。
- 每 process 最多 8 sessions；每 session 一個 active logical turn、30 分鐘 idle
  expiry、最多 16 logical turns、最多 32 recovery records。
- scope ID 是 process-lifetime tombstone：同一 process 內即使 session close、delete
  或 expire 也不可 reuse；並行 create/replay 最多一個成功。process restart 的新
  runtime key 令舊 handles 失效，session route 回 `410 session_gone`。

除 `POST /v1/sessions` 外，所有 route 要求
`Authorization: Bearer <session_capability>`。MVP bearer 是 transport capability，
不是 production user authentication/tenancy 的替代品。

固定 versioned routes：

```text
POST   /v1/sessions
POST   /v1/sessions/{id}/messages
POST   /v1/sessions/{id}/turns/{turn_id}/cancel
GET    /v1/sessions/{id}/turns/{turn_id}
GET    /v1/sessions/{id}/events
POST   /v1/sessions/{id}/approvals/{approval_id}
DELETE /v1/sessions/{id}
```

一個 session 有 active `running` 或 `awaiting_approval` turn 時，新的 user prompt
必須回 conflict；delete/idle expiry dispose Pi/session state、close scope，但絕不改寫
canonical data 或撤回 durable refresh。

### SSE, replay and recovery

SSE event schema 為 `agent_event.v1`，包含 session-monotonic sequence、event ID、
session/turn ID、safe timestamp 和 type-specific safe payload。每 session 保留 256
events 或 2 MiB（先到者為準）的 ring。`Last-Event-ID` 支援 ordered replay；cursor
已被 evict 時 client 使用 turn recovery endpoint，process restart 則 `410`。

允許的 event types 精確為：

```text
session.started
turn.started
message.delta
tool.started
tool.completed
approval.required
approval.resolved
artifact.final
turn.completed
turn.failed
```

Logical turn states 是 `running | awaiting_approval | completed | cancelled | failed`。
每 turn 只能有一個 terminal event：`turn.completed`（包括 cancelled state）或
`turn.failed`。有 artifact 時 `artifact.final` 必須先於 terminal；failed/cancelled
不得捏造 final artifact。late SSE connection、replay、recovery、cancel 和 queue
interleaving 都必須維持上述 sequence/order invariant。

## Phase 2c hidden ONSPD approval

ONSPD 只在 test policy 額外開放 `uk.postcode-resolution` capability/profile；production
policy 及 prompt fixture 一律維持 Bank Rate only。當 Phase 1
`request_data_refresh` 回 `approval_required` 時：

1. Runtime 以 `approval.required` 事件通知 UI，logical turn 進入
   `awaiting_approval`，拒絕新 user prompt。confirmation token 仍只在 trusted Python
   data plane，絕不進 Node/model/SSE/log。
2. `POST /v1/sessions/{id}/approvals/{approval_id}` 只接受 approve/deny；host 驗證
   bearer、session、principal、scope、approval fingerprint/version、expiry 和
   one-decision replay semantics，再呼叫 hidden `approve_refresh`。
3. approve 時若 Pi 仍在 streaming，將 follow-up 排在目前 stream 後；若 Pi 已 idle，
   以 triggered custom message 恢復**同一 logical turn**。不得開新 user turn 或新
   capability scope。
4. deny 不觸發 model；直接發 `approval.resolved` 與唯一 terminal event。
5. expiry、replay、opposite decision、scope/principal/policy/version mismatch 皆 fail
   closed，並保留 last-good canonical data。

## Product acceptance mapping and deterministic fixtures

[`tests/Test case.md`](../../tests/Test%20case.md) 是產品 acceptance catalog，不能
被本 vertical slice 改寫為「十題均已支援」。Phase 2 的 mapping 為：

| Product case | Phase 2 treatment |
|---|---|
| TC-01 Prime rent | blocked-coverage representative；最多 `describe_market_data`，不 query/refresh、不產數字或 citation |
| TC-02–04、TC-06–08、TC-10 | deferred product acceptance，等待對應 canonical capability |
| TC-05 Interest-rate impact | Bank Rate partial vertical slice；macro fact 與不確定 inference 可執行，但不是完整 macro/rent analysis |
| TC-09 Daily brief | opt-in composite smoke，不能取代 deterministic gate 或宣稱 complete coverage |

Phase 2a 的 direct runner 必須以官方 `fauxProvider`、完全 offline 的 seeded canonical
Bank Rate，固定下列七個 deterministic prompt fixtures：

1. **Ambiguous date**：先 clarification，零 data calls。
2. **Explicit historical/as-of**：query + citation，partial brief，不 refresh。
3. **Latest and fresh**：query/citation，不 refresh。
4. **Latest stale, refresh succeeds**：request、bounded polls、terminal canonical re-query。
5. **Refresh failed/dead-letter**：保留 last-good，產 stale/degraded/partial。
6. **No canonical**：unavailable，不 direct-fetch。
7. **TC-01 blocked**：最多 describe；不 query/refresh、不產 numeric/citation，只回
   generic current-launch coverage unavailable。

各 fixture 另驗證 tool count、byte/record/citation budget、call ordering、turn state、
finalizer ledger lineage、numeric guard 和 terminal ordering。資料不足的輸出必須明確
coverage/freshness warning，不能以 model prose 填數字。

### Verification gates

Phase 2 implementation 完成時至少執行並保存下列獨立 evidence：

1. `npm ci`、Node engine、exact dependency lock、Pi startup fail-fast、loader/Skill
   hash/size/symlink/extra-file rejection，以及 exact active-tool set。
2. Launcher single-JSON/FD3/absolute-binary/allowlisted-env tests；malformed,
   overflow, timeout, cancel/crash 的 process-group cleanup；read selector no-writer
   proof；Ajv catalog/schema/exit-code parity。
3. Faux direct-runner tests：七個 fixtures、45-second turn deadline、poll cadence、
   all budgets、terminal re-query、no direct fetch、no raw model numeric stream。
4. Finalizer/ledger tests：unknown/cross-session/cross-anchor/duplicate/unresolved ref、
   numeric citation hydration、published-at-null warning、fact/inference/datasource
   confidence separation、Unicode/number-word adversarial corpus。
5. HTTP/SSE tests：bearer hash/constant-time check、session limits/idle expiry/no-reuse
   scope、sequence/replay/eviction/recovery/cancel/single terminal/artifact ordering。
6. Phase 2c tests：ONSPD approve/deny, expiry, same/opposite replay, scope/principal/
   fingerprint/version mismatch, token non-disclosure, same-session continuation, and
   production-policy exclusion.
7. `RUN_REAL_MODEL_SMOKE=1` 才執行 real-model smoke；它使用 seeded canonical
   Bank Rate、禁止 live ingestion，且永遠不能替代以上 deterministic gates。

## Phase 2 completion criteria

Phase 2 只有在 2a、2b、2c 全部通過後才能把 `implementation_status` 改為 complete。
完成時可宣稱的範圍僅為：一個受控 Pi session 可從 canonical Bank Rate 產生可回放、
host-hydrated、citation-grounded、明示 partial coverage 的 `market_brief.v1`。

不得宣稱：ONSPD 已 production-enabled、TC-01–TC-10 全部可回答、Agent 有 filesystem
or general web research capability、或 Pi service 是 canonical writer/security-complete
production service。

## References

- [[wiki/decisions/agent-tool-facade-foundation|Agent Tool Facade Foundation]]
- [[wiki/architecture/agent-runtime|Agent Runtime Architecture]]
- [[wiki/architecture/data-access-freshness|Data Access and Freshness Architecture]]
- [[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status]]
- [[wiki/research/agent-skill-and-tool/skill-and-tool-design|Agent Runtime, Skill and Tool Research]]
- [Pi Agent Harness](https://github.com/earendil-works/pi)
- [Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)
- [Ajv format validation](https://ajv.js.org/guide/formats)
