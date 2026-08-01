---
type: wiki
updated: 2026-08-01
status: accepted
source: "[[wiki/decisions/agent-tool-facade-foundation|Agent Tool Facade Foundation]]"
tags: [agent, runtime, pi, skills, mvp]
---

# Pi Agent Runtime and Skills Vertical Slice

> **Implementation state: planned.** 本Decision只有在
> [[wiki/decisions/agent-tool-facade-foundation|Agent Tool Facade Foundation]]
> exit gate通過後才進入工程整合。

## Decision

第二階段以Pi原生SDK建立一個Market Analyst Agent，接上第一階段五個typed
data Tools，並完成Bank Rate grounded brief vertical slice。

採用：

- Node `>=22.19.0`。
- Exact `@earendil-works/pi-coding-agent@0.83.0`；2026-08-01已以
  `npm view`確認registry可取得。
- Programmatic `createAgentSession()`，不是Pi RPC subprocess。
- 單一top-level AgentSession和bounded logical sub-tasks。
- Explicit model、resource、Tool、product capability和Skill allowlists。
- SSE product events、process-lifetime sessions和runtime-only structured brief
  validation。

不新增competition profile、generic policy engine、task manager、自訂
`before_agent_start`或recursive child-agent framework。

## Scope

### In scope

- Minimal Node／TypeScript Agent Service。
- Session、turn、cancel、SSE replay和approval lifecycle。
- Rooted read-only resource tools及strict Pi resource loading。
- 五個Agent data Tools的Pi `defineTool()` adapters。
- 兩個首批Skills。
- Model-authored `market_brief_draft.v1`、host-enriched `market_brief.v1`和
  runtime-only `finalize_market_brief`。
- Deterministic fake-model integration tests及一個real-model offline smoke。

### Deferred

- Durable session persistence、personnel auth、tenancy和multi-user isolation。
- WebSocket、model switching／cycling和provider fallback。
- Non-canonical `result_ref`／ad-hoc research result consumption。
- Agent artifact／claim persistence。
- Full dashboard implementation和production deployment hardening。
- Product-blocked data capabilities及第二批Skills。

## Runtime baseline

### Package and model

Package鎖定：

```json
{
  "engines": {"node": ">=22.19.0"},
  "dependencies": {
    "@earendil-works/pi-coding-agent": "0.83.0"
  }
}
```

Implementation必須提交lockfile，以clean `npm ci`證明exact package及transitive
dependencies可安裝；不依賴GitHub `main`內容代替published artifact。

Model必須由`PI_MODEL=<provider/model>`明確設定。Runtime使用Pi
`ModelRuntime`標準authentication resolution，但：

- 未設定、model不存在或沒有authentication時fail startup。
- 不自動選第一個available model。
- Competition service不提供model cycling。
- Tests使用deterministic fake model，不依賴real credentials。
- Real-model smoke明確opt-in；credential path不在Agent readable resource root。

### Session and capabilities

- `SessionManager.inMemory()`。
- 一個session只允許一個active turn；第二個user prompt返回`409 conflict`。
- Session建立至少128-bit random `session_capability`和獨立
  `capability_scope_id`；兩者均不進model context或SSE。
- Process restart、explicit delete或30分鐘idle expiry會dispose session及撤銷scope；
  canonical data不受影響。
- 支援`session.abort()`；cancel後每個turn只發一個terminal event。
- 一個Market Analyst Agent自行拆解bounded sub-tasks，不建立child session。
- Host follow-up一次只排在current active turn之後，不與user turn並行。

Phase 1的`cursor_ref`、`citation_ref`、`job_ref`和`approval_id`全部以
`capability_scope_id`驗證。Runtime不得把raw cursor、job ID、`result_ref`或
confirmation token放進model messages。

### Resource isolation and Skills

Competition service使用專用read-only `agent_resource_root`，不是repository root。
部署時只copy：

- `skills/track-uk-macro/SKILL.md`。
- `skills/generate-grounded-market-brief/SKILL.md`。
- Optional generated `wiki/market/` projection。

Staging拒絕symlink、dotfile、SQLite、CAS／raw evidence、config、credentials和
operator docs。Root內檔案有size bound並在startup核對manifest hash。

Pi resource setup：

- `SettingsManager.inMemory()`，不讀global／project settings或packages。
- Dedicated empty `agentDir`和不在Git repository內的`cwd`。
- `skillsOverride`**取代**discovered Skills，只回傳上述兩個allowlisted Skills。
- `agentsFilesOverride`和`promptsOverride`回傳empty；不載入AGENTS／CLAUDE files
  或prompt templates。
- 不提供extension factories或package sources；`reload()`後若extensions、
  unexpected Skills、context files或prompts非空，startup fail closed。
- `systemPromptOverride`只使用version-controlled Market Analyst prompt。

不註冊Pi unrestricted filesystem built-ins。Runtime以Pi的read／grep／find／ls tool
factories配合custom filesystem operations建立同名rooted adapters。Pi Skill prompt
會使用`Skill.filePath`的absolute location，因此adapter允許absolute或relative input，
但relative一律以`agent_resource_root`解析，absolute只允許在該root內；每次先
`lstat`／`realpath`並核對path containment，拒絕`..` escape、任何symlink和root外
absolute path。`bash`、`edit`、`write`完全不註冊。Generated Wiki只能作discovery；
正式數值必須由`query_market_data`驗證。

Startup最終核對registered tool names等於四個rooted resource tools、五個Phase 1
data tools和`finalize_market_brief`；任何extension-added或unknown tool均fail。

## Product transport

### HTTP and session capability

- `POST /sessions`：建立session，回`session_id`、host-only
  `session_capability`和events URL。
- `POST /sessions/{session_id}/messages`：接受prompt後回
  `202 {session_id, turn_id, events_url}`。
- `POST /sessions/{session_id}/cancel`：abort active turn。
- `GET /sessions/{session_id}/turns/{turn_id}`：回目前／terminal turn state及
  final artifact，供recovery。
- `POST /approvals/{approval_id}`：body只允許
  `{"decision":"approve"|"deny"}`。
- `GET /sessions/{session_id}/events`：SSE stream。
- `DELETE /sessions/{session_id}`：dispose session並撤銷scope。

除`POST /sessions`外，每個route都要求
`Authorization: Bearer <session_capability>`，並驗證session／scope binding。
Competition MVP沒有personnel identity、role directory或tenancy；state-changing
requests只接受same-origin JSON且不用cookie authentication。這個ephemeral
session capability不是production auth的替代品。Browser UI以`fetch()` streaming
讀SSE以附上Authorization header；capability不放query string，也不使用無法自訂
header的native `EventSource`。

### SSE events and recovery

最小event types：

- `session.started`
- `turn.started`
- `message.delta`
- `tool.started`
- `tool.completed`
- `approval.required`
- `artifact.final`
- `turn.completed`
- `error`

每個event包含`event_id`、monotonic `sequence`、`session_id`、nullable
`turn_id`、`trace_id`和RFC3339 UTC timestamp；只有`session.started`的
`turn_id`可為null。每個turn只能有一個terminal
`turn.completed`或`error`。

Runtime為每個session保留最近256個events，並在獨立in-memory turn record保留
terminal status、final assistant message和`market_brief.v1`直到session expiry。SSE支援
`Last-Event-ID` replay，因此message POST早於stream連線不會丟event。Cursor已
evict時回`409 event_cursor_expired`，client改用turn recovery endpoint；process
restart後session route回`410 session_gone`。

## Tool registration and runtime budgets

使用Pi `defineTool()`／`customTools`註冊：

- `describe_market_data`
- `query_market_data`
- `get_citation_metadata`
- `request_data_refresh`
- `get_refresh_status`
- Runtime-only `finalize_market_brief`

Node adapter只執行第一階段fixed binary、傳入host context、解析一個JSON result並
再次schema validate；不把stderr、stack或raw child output送入model。Facade
`<tool-name>` argv是唯一selector。

Launch host policy固定：

- `allowed_capability_ids = ["uk.bank-rate-current"]`。
- `allowed_refresh_profiles = ["bank-rate-latest"]`。
- Fixed `competition-agent` principal和`open` access class。
- `capability_scope_id`等於當前session的host-owned random scope。
- 每個refresh以`turn_id + tool_call_id + random suffix`建立獨立
  `refresh_request_id`，放入Phase 1 wire的host-only
  `host_context.refresh_request_id`；process retry／approval replay才沿用。

ONSPD hidden integration使用獨立test host policy，只額外allow
`uk.postcode-resolution`和`onspd-one-postcode`；production launch policy不變。
該capability沒有query Tool route，test以seeded quota／fake broker走完整
approval UI和host-only replay後即dispose session，不會把ONSPD變成launch feature。

Per-turn cumulative bounds：

- 最多8個data Tool calls，包含status calls。
- 每次query／citation最多20 items；全turn最多40 records和40 citations。
- Model-visible Tool JSON全turn累計最多128 KiB；單一Facade response仍受256 KiB
  protocol bound。
- 最多3次status polls、最多2次`finalize_market_brief` attempts。
- Facade subprocess timeout 10秒；整個turn wall-clock 45秒。
- Model response最多4096 output tokens。

Adapter在注入model context前按UTF-8 bytes和item counts原子檢查；超出累積budget的
整個result不注入，回`TOOL_BUDGET_EXCEEDED`，不截斷成invalid JSON。這些bounds
是local request accounting，不建立generic policy engine。

### Refresh polling and cancellation

- Refresh ack的`poll_after_seconds`是每兩次status call之間的minimum cadence。
  Adapter在ack後及每次pending status response後，以host monotonic timer重設
  `next_poll_not_before`；不得讓model連續立即消耗三次poll。
- 一個turn最多等待refresh 15秒且不超過45秒turn deadline。
- 三次poll或wait budget後仍pending，停止poll並以last-good canonical／health完成
  `partial` answer；不啟動background model turn。
- 後續user turn可用同session `job_ref`再查；terminal後必須canonical re-query。
- Cancel會abort Pi、終止active Facade child process group；已durably enqueue的
  refresh job繼續由workflow運行，不回滾。

## ONSPD approval lifecycle

ONSPD profile不在第一個launch allowlist，但Phase 2必須完成hidden integration
test。`confirmation_required`不阻塞current Tool call：

1. Runtime只保存`approval_id`和session binding；Phase 1 immutable approval mapping
   保存normalized exact request snapshot、`refresh_request_id`、policy version和expiry，
   broker token仍留在trusted data plane。
2. Model只收到`approval_required`及opaque `approval_id`。
3. SSE發`approval.required`，current turn以pending結束。
4. UI以同session bearer向`POST /approvals/{approval_id}`明確approve或deny。
5. Host驗證session／principal／scope／expiry／one-time state，再呼叫Phase 1
   host-only `approve_refresh`。
6. Approved acknowledgement以一個host follow-up turn送入同session；若已有active
   turn便FIFO排在其後。Denied只發terminal approval state，不觸發model。

Runtime／process restart後舊session capability失效，即使data plane保留未過期
approval mapping也回`410`；使用者需建立新request。Agent不能自行批准或取得
confirmation token。

## First Skills

### Rewrite `track-uk-macro`

保留macro domain interpretation，移除所有direct datasource imports、browser、
upstream URL和operator CLI instructions。固定工作流：

1. 時間會實質改變答案但未指定時，先追問。
2. 查`uk.bank-rate-current` canonical health／metric。
3. 明確要求latest且stale／missing時，才申請`bank-rate-latest` refresh。
4. Host-timed bounded status polling；terminal後重新query canonical。
5. 以query回傳的`citation_ref` resolve citation metadata。
6. 保留source period、geography、unit、definition、freshness和limitations。
7. UK／London facts分開，不把macro proxy當London office rent或transaction。

### Add `generate-grounded-market-brief`

只保存非顯然、可重用的分析規則：

- 把fact、inference和limitation分開。
- Fact必須使用本turn已resolve的`supporting_citation_refs`；observation／evidence
  identity和source metadata由runtime ledger導出。
- Inference引用supporting fact IDs並保留confidence／caveat。
- 不把時間相關性寫成因果。
- Inference不引入數字、number words、basis-point或quantitative comparison；衍生計算
  待deterministic calculation Tool。
- Coverage不足時輸出`partial`或`unavailable`，不填補缺失值。
- 最後調用`finalize_market_brief`。

Tool schemas、runtime budgets、datasource registry和長JSON examples不複製進
Skills。

## Structured brief

`finalize_market_brief`是runtime validation Tool，不是datasource writer。Model只可
提交：

```text
market_brief_draft.v1
- title
- as_of
- status: complete | partial | unavailable
- facts[]:
  - claim_id
  - kind: numeric_observation | qualitative
  - confidence: high | medium | low
  - supporting_citation_refs[]
  - text: required only for qualitative
  - numeric: required only for numeric_observation
    - citation_ref
    - value: JSON string or number
    - unit
    - definition
    - as_of
    - source_date: ISO date or null
    - period_label: string or null
- inferences[]:
  - claim_id
  - text
  - confidence: high | medium | low
  - supporting_fact_ids[]
  - caveat
- limitations[]
- freshness_warnings[]
```

Validation rules：

- Runtime為本turn成功的query／citation results保存host-only ledger：
  `citation_ref -> canonical anchor/run + observation_id + evidence_id + Facade numeric
  projection + safe metadata`。Runtime不得自行猜payload key；Bank Rate value來自
  Phase 1 manifest固定的`bank_rate_percent` selector。
- 每個fact至少一個`supporting_citation_ref`；每個ref必須在ledger中已完成
  `get_citation_metadata`解析，且scope、anchor、run、observation和evidence關係完全
  相同，不能只做ID set membership。
- Model不能提交publisher、URL、locator、observation ID或evidence ID；避免已知ID配
  錯metadata或把無關observation／evidence交叉配對。
- `numeric_observation`不得提交自由文字；其`citation_ref`必須同時出現在
  `supporting_citation_refs`。Validator以canonical JSON scalar equality比較value，
  並exact比較unit、definition、query-anchor `as_of`、source date和period label；
  任一不符即拒絕。Unit或definition在record中為null時，不可形成material numeric
  fact，只能返回`partial`／`unavailable`。
- Draft `as_of`必須exact等於本turn ledger的一個query anchor；對`complete`／
  `partial`，所有facts的refs必須屬於同一anchor。`unavailable`且沒有facts時，
  `as_of`必須等於本turn最後一次Bank Rate query的anchor。Host從matched ledger
  entry產生artifact `as_of`，不沿用model string；若refs來自不同anchor，Agent必須
  以單一明確`as_of`重新query後再finalize。
- `qualitative` fact必須提交text且不得提交numeric。Competition v1拒絕所有
  model-authored display text（title、qualitative fact、inference／caveat、limitations、
  freshness warnings）內的Unicode number、percent或currency tokens；derived
  percentage、change或comparison須等deterministic calculation Tool，不能以free
  text繞過numeric validation。這是runtime lexical guard，不宣稱能理解所有number
  words或quantitative comparison；`five basis points`、`twice as high`及同類語義由
  Skill禁止，並由acceptance evaluator的固定adversarial corpus拒絕。
- 每個inference至少引用一個existing fact。
- Unknown IDs、missing lineage、duplicate claim ID或invalid status均deterministic
  reject。
- Causal wording屬Skill及test evaluator規則；不宣稱JSON validator能理解自然語言
  語義。

成功後host按ref首次出現順序確定性產生`market_brief.v1`。Model-authored欄位沿用
draft；identity欄位固定為：

```text
facts[].supports[]:
  - citation_ref
  - observation_id
  - evidence_id
facts[].numeric:
  - value
  - unit
  - definition
  - as_of
  - source_date
  - period_label
sources[]:
  - citation_ref
  - canonical_anchor
  - canonical_run_id
  - observation_id
  - evidence_id
  - publisher
  - title
  - public_url
  - published_at
  - retrieved_at
  - locator
```

Artifact top-level `as_of`、numeric fact的顯示text和上述identity／numeric欄位全部
由ledger確定性產生；
citation metadata沿用Phase 1的required／nullable規則，model-authored文字不能覆寫
identity、numeric value或metadata。Artifact
只保存在in-memory turn record，發`artifact.final`並可由turn recovery endpoint
讀取；不寫canonical DB、data-plane `output_artifact`或projection files。

## First vertical slice

User question：

> 「截至指定時間，英國 Bank Rate 是多少？它對倫敦辦公室市場可能代表甚麼？」

Expected flow：

```text
query health / canonical metric
  -> optional request refresh
  -> host-timed bounded status
  -> canonical re-query
  -> exact citation metadata
  -> track-uk-macro interpretation
  -> generate-grounded-market-brief
  -> finalize_market_brief
  -> SSE message + recoverable artifact
```

Rent和investment transaction尚無approved canonical inputs時，結果必須標為
`partial`。這條slice驗證TC-05的macro facts、fact／inference和citation
behaviour，不宣稱完整產品coverage。

## Deferred Skill rollout

Tool Facade穩定後第二批：

1. 重寫`check-office-esg`，只使用MHCLG EPC proxy。
2. 把`assess-office-demand`縮窄為`assess-hybrid-working-signal`。
3. 把`map-london-submarkets`改為`resolve-london-geography`，再把ONSPD profile
   加入launch allowlist。
4. 從`collect-office-market-metrics`拆出`track-office-stock`，只使用VOA。

保持停用至source blockers關閉：

- `monitor-market-news`
- `track-office-supply`
- Broad `collect-office-market-metrics`
- Submarket comparison、vacancy anomaly、event investigation和full
  daily／quarterly reports
- Non-canonical ad-hoc／discovery result consumption

## Verification and exit gate

以deterministic fake model為主要gate：

1. `npm ci`由lockfile安裝published Pi `0.83.0`；Node engine不符時fail。
2. Loader只載入兩個allowlisted Skills、system prompt和零extensions／context／
   prompts；unexpected resource或tool令startup fail。
3. Rooted read／grep／find／ls可讀allowlisted Skill／Wiki的relative path及Pi提供的
   in-root absolute Skill location；`..` escape、root外absolute path、symlink、
   `.env`、SQLite、CAS、config和credential paths全部拒絕。
4. Fresh canonical：不refresh，numeric fact的value／unit／definition／as-of、source
   和brief引用相同canonical anchor／run／observation／evidence；任何值錯配被拒絕。
5. Historical as-of citation保持當時canonical run，不漂移到較新evidence。
6. Stale＋refresh accepted：ack含`job_ref`和`poll_after_seconds`；ack及每次pending
   status後均重設host monotonic timer，三次poll不burst。Terminal後re-query，不把job
   payload當市場數據。
7. Refresh denied／timeout／dead-letter／poll budget exhausted：保留last-good並標
   stale／degraded／partial。
8. No canonical：回unavailable，不direct-fetch。
9. Fact缺citation ref、unknown ref、跨anchor／run的observation-evidence錯配、
   numeric value／unit／definition／date錯配、Unicode numeric／percent／currency
   lexical bypass或model提交的source metadata均由validator拒絕；artifact numeric
   text和metadata由host ledger產生。固定evaluator另拒絕number-word／quantitative
   comparison及causal wording adversarial cases，不宣稱runtime有通用semantic
   detection。
10. Citation內prompt injection不能改tool、profile、URL或runtime instructions。
11. Arbitrary URL、Shell、SQL、write／promote request沒有可用Tool。
12. Malformed、oversized、timeout或crashed child安全終止process group並轉成typed
    error；cumulative record／byte／time budgets受測。
13. SSE ordering、late-connect replay、`Last-Event-ID`、eviction recovery、cancel
    和single terminal event受測。
14. ONSPD用test-only `uk.postcode-resolution` host policy走same-session UI action；
    token不進model、stdout、stderr或events，normalized snapshot可在Facade subprocess
    exit／restart後重建exact request，expiry／policy change／replay／scope change
    fail closed；Node Runtime restart仍按session loss回`410`。
15. Session A的cursor／citation／job／approval handle不可由Session B使用。
16. Product-blocked問題返回manifest coverage blocker。
17. Pi service停止時，獨立Python consumer仍能用`query_data_v1`讀canonical
    Bank Rate；不以尚未實作的dashboard作gate。

最後另跑一個real-model、offline-seeded smoke。Real network ingestion維持獨立
opt-in，不成為Runtime test預設。

Exit gate是：一條Bank Rate問答可由Pi session經typed Tools產生schema-valid、
有exact citation、明確partial coverage且可經SSE replay／turn endpoint恢復的
`market_brief.v1`，而Agent沒有data-plane writer、unrooted filesystem、
extension或blocked datasource capability。

## References

- [[wiki/decisions/agent-tool-facade-foundation|Agent Tool Facade Foundation]]
- [[wiki/research/agent-skill-and-tool/skill-and-tool-design|Agent Runtime, Skill and Tool Research]]
- [[wiki/architecture/agent-runtime|Agent Runtime Architecture]]
- [[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status]]
- [Pi package on npm](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
- [Pi package manifest](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json)
- [Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)
- [Pi Skills](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md)
- [Pi Extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
- [Pi RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
