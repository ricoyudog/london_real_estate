---
type: wiki
updated: 2026-08-02
status: accepted
implementation_status: complete
source: "[[wiki/research/agent-skill-and-tool/skill-and-tool-design|Agent Runtime, Skill and Tool Research]]"
tags: [agent, tools, facade, contracts, datasource]
---

# Agent Tool Facade Foundation

> **Implementation state: complete (2026-08-02).** `accepted` 表示本設計已採納；
> 本文件末段記錄本次 Phase 1 工程交付與 exit-gate 證據。Pi Runtime integration
> 仍維持 deferred。

## Decision

第一階段先建立獨立 **Agent Tool Facade**，再開始 Pi Runtime integration。Facade
是 Node／Pi 與已完成 Python data plane 之間唯一受支援的 Agent capability
boundary：

- 只包裝 `query_data_v1`、`request_refresh_v1`、
  `get_refresh_status_v1` 和本階段新增的安全 catalog／citation projections。
- 不把 operator `cre`、datasource adapters、repository、`OperationalStore`、
  raw evidence、SQL、collector、review 或 promotion 暴露給 Agent。
- Model 只控制各 Tool schema 明列的 arguments；identity、session scope、access、
  product capability、profiles、budgets、lane、licence、retention 和 promotion
  由 trusted host 固定。

本 Decision 依賴
[[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource
Operational Implementation Status]]。第二階段
[[wiki/decisions/pi-agent-runtime-and-skills-vertical-slice|Pi Agent Runtime and
Skills Vertical Slice]] 必須等待本 Decision exit gate 通過。

## Scope

### In scope

- Agent-only process／JSON protocol。
- 五個 model-facing data Tools。
- Versioned product capability manifest和packaged refresh profile catalog。
- Safe canonical query和exact citation metadata contracts。
- Session-scoped cursor／citation／job／approval capabilities。
- Refresh request／status、host-only approval control及ONSPD human approval
  handoff。
- Input、output、pagination、timeout和error bounds。
- Python contracts、facade、packaging及non-Python consumer fixtures。

### Deferred

- Pi session、SSE、Skills和model configuration。
- Non-canonical `result_ref` handoff、ad-hoc／discovery result consumption。
- Deterministic series analysis、submarket comparison和anomaly detection。
- Agent artifact persistence、chart／table writer或canonical projection publish。
- Product-blocked rent、vacancy、news、project-level supply和broker submarket data。
- Production auth、tenancy、container sandbox和remote multi-user API。

## Process boundary

安裝一個獨立 executable：

```text
nan-fung-agent-tools <tool-name>
```

MVP每次Tool call啟動一次bounded subprocess：

- 固定binary和argv array，`shell: false`。
- `<tool-name>` argv是唯一authoritative selector；JSON內不重複`tool`欄位。
- stdin是UTF-8、EOF-terminated的單一JSON object，最多64 KiB；unknown top-level
  fields、trailing non-whitespace bytes或invalid UTF-8一律拒絕。
- stdout只可有一個UTF-8 JSON result，最多256 KiB；logs只寫stderr且capture最多
  64 KiB。stdout含多餘bytes即為protocol error。
- Default subprocess timeout 10秒；request／status不在subprocess內長輪詢。
- Timeout或cancel先終止整個child process group，1秒後仍未退出則kill。
- Read-only calls不持有writer config；refresh經separately permissioned broker。
- Process startup latency須以profiling證明成為瓶頸，才可在不改wire schemas下轉成
  long-running service。

Operator `cre` 維持local operations surface，不是這個binary的alias或subcommand。
Binary另有一個host-only `approve_refresh` operation；它不出現在Pi Tool allowlist。

`_runtime_facade(tool_name)` 依 selector 建構最小 dependency graph，不能因一個
generic constructor 令 canonical read child 取得 writer surface：

| Selector | Permitted dependencies |
|---|---|
| `describe_market_data` | `ReadService` |
| `query_market_data` | `ReadService`、citation repository |
| `get_citation_metadata` | citation repository |
| `request_data_refresh` | `ReadService`、broker、approval store |
| `get_refresh_status` | broker |
| `approve_refresh` | broker、approval store |

前三個 canonical-read processes 不得建立 `OperationalStore`、
`OperationalRefreshBackend` 或 `RefreshBroker`。這是 process-level isolation，
不只是不使用某個方法的 convention。

## Shared wire contract

### Request

```json
{
  "schema_version": "agent_tool_request.v1",
  "request_id": "call_...",
  "arguments": {
    "capability_id": "uk.bank-rate-current",
    "datasource_id": "boe.bank_rate.iudbedr",
    "request_profile": "bank-rate-latest",
    "bounded_scope": {},
    "intent": "user_requested_latest"
  },
  "host_context": {
    "principal": "competition-agent",
    "capability_scope_id": "scope_...",
    "turn_id": "turn_...",
    "tool_call_id": "toolcall_...",
    "refresh_request_id": "refresh_...",
    "allowed_access_classes": ["open"],
    "allowed_capability_ids": ["uk.bank-rate-current"],
    "allowed_refresh_profiles": ["bank-rate-latest"]
  }
}
```

`request_id`是每次subprocess invocation ID。`host_context`由Runtime組裝，不出現
在Pi Tool parameter schema；`capability_scope_id`是每個session以至少128-bit
random建立的不可猜scope。`refresh_request_id`只對`request_data_refresh`必填，
read／status operations必須省略；它在同一logical refresh的process retry中保持
不變。Facade拒絕arguments內任何principal、scope、access、capability policy、
profile policy、lane、URL、definition、retention、promotion或refresh identity欄位。

`request_data_refresh`的`refresh_request_id`由host建立，不得以`request_id`、
`turn_id`或`tool_call_id`代替；Facade把它映射到
`RefreshContext.request_instance_id`。相同logical refresh的process retry及
approval replay沿用它，另一個refresh即使在同一turn也必須使用新ID。

### Scoped capability handles

Model可傳回的stateful references只有：

| Handle | Binds |
|---|---|
| `cursor_ref` | query fingerprint、最後一筆實際emit的boundary、principal、scope、expiry |
| `citation_ref` | anchor、canonical run、observation、evidence、locator hash、principal、scope |
| `job_ref` | durable job ID、principal、scope、expiry |
| `approval_id` | normalized exact refresh request snapshot、principal、scope及10-minute expiry |

前三者使用versioned authenticated encoding及Runtime-boot-scoped 256-bit HMAC key；
每次使用都驗證kind、principal、`capability_scope_id`、expiry及request binding。
同一Runtime的所有one-shot Facade children經dedicated inherited read-only FD取得同一
key，讀取後立即close；key不放argv、environment、request JSON、disk或logs。
Runtime restart會rotate key，而Phase 2同時令舊session及handles失效。
`approval_id`使用durable mapping。Raw read cursor、job ID、`result_ref`和
confirmation token不作model schema欄位。`AgentToolHost` 對每個已開啟 scope 保留
process-lifetime tombstone：session close、delete 或 expiry 後仍不可重用同一 scope；
lock-protected concurrent duplicate 只有一個可成功。另一 session 即使共用
`competition-agent` principal也不能重播handles。Competition handles
最長30分鐘；`approval_id`沿用data plane的10分鐘expiry。

### Result

```json
{
  "schema_version": "agent_tool_result.v1",
  "request_id": "call_...",
  "status": "ok",
  "data": {},
  "warnings": [],
  "error": null
}
```

`status`只允許`ok`、`partial`、`error`。Error包含stable `code`、safe
`message`和`retryable`，不得包含stack、SQL、local path、credentials、headers
或raw upstream body。

Process exit codes：

| Exit | Meaning |
|---|---|
| `0` | schema-valid `ok`／`partial` |
| `2` | invalid request |
| `3` | access／policy denied |
| `4` | retryable unavailable |
| `5` | internal failure |
| `6` | protocol／schema violation |

Stable error codes：

| Code | Exit |
|---|---|
| `INVALID_ARGUMENT`／`INVALID_CURSOR` | `2` |
| `ACCESS_DENIED`／`CAPABILITY_BLOCKED`／`POLICY_DENIED` | `3` |
| `RETRYABLE_UNAVAILABLE`／`TIMEOUT` | `4` |
| `INTERNAL_ERROR` | `5` |
| `SCHEMA_VIOLATION`／`PROTOCOL_ERROR`／`RESULT_TOO_LARGE` | `6` |

### Cross-language selector catalog

除 generic request/result envelope 外，wheel 另隨附 Draft 2020-12
`agent_tool_contracts.v1.json` 和其 catalog schema。catalog 是 Node/Pi 等非 Python
consumer 對 argv selector 的唯一 machine-readable authority；它覆蓋五個 model-facing
selectors 和 host-only `approve_refresh`，而 selector 仍只存在 argv，不在 request JSON
新增重複的 `tool` 欄位。

每個 selector contract 固定：audience、arguments schema、successful `data` schema，
以及 host-context `refresh_request_id` 的 `required|forbidden` policy。所有正常 nested
object 預設 `additionalProperties: false`；`geography`、record `payload`、citation
`locator` 和 refresh `bounded_scope` 是明確 named opaque projections，仍有 property
count、key、scalar/array item 與 length bounds，不能接收不受限 nested object。

generic result schema 以 conditional schema 鎖定 `ok|partial` 必有 object `data` 和
`error: null`，`error` 必有 `data: null`；每個 error code、safe message、retryable 和
exit mapping 與 Python `_ERROR_DETAILS` 完全一致。`jsonschema>=4,<5` 僅是 dev
dependency；runtime loader 不依賴它，仍會 strict-load package asset、reject duplicate
JSON keys 並比對 facade 的 exact model/host selector sets。

`tests/fixtures/agent_tools/v1/tool-contract-fixtures.json` 提供 language-neutral
valid、invalid、policy-invalid 和 partial examples。Python `Draft202012Validator` 同時
驗證 catalog、generic envelope、selector schemas、fixtures 與 actual Facade projection；
這讓 future Node launcher 可用同一份資產作 Ajv validation。

## Product capability authority

隨wheel安裝
`src/nan_fung/agent_tools/capabilities.v1.json`。它是Agent product coverage的
machine-readable authority，不解析Markdown，也不由datasource `operational`
狀態自動推導。每個entry固定：

- `schema_version`、`capability_id`和`status: supported|partial|blocked`。
- Allowed query template或明確`query_disabled: true`、datasource／metric／geography
  scope和refresh profiles。
- Numeric capability的fixed direct payload-field selector和scalar type；不允許
  JSONPath、expression或model-supplied field name。
- Limitations、blocked reason和owner。

初始manifest至少把`uk.bank-rate-current`標為`supported`，並固定
`numeric_value_field: bank_rate_percent`、`numeric_value_type: decimal_string`；
Bank Rate對London office market的interpretation標為`partial`。另保留非launch的
`uk.postcode-resolution` partial capability：沒有query template，只允許
`onspd-one-postcode` refresh profile，並明示只供Phase 2 hidden approval integration
及未來`resolve-london-geography` rollout。Prime rent、vacancy、ranked news、
project supply、broker submarkets及transactions標為`blocked`。Registry只補充
live freshness和availability；Engineering PASS不能提升product status。

## Model-facing Tools

### `describe_market_data`

只回傳host `allowed_capability_ids`與manifest交集的safe projection，加上目前
canonical availability和freshness：

- `capability_id`、status、可用query kind、datasource／metric IDs和geography。
- Allowed refresh profile、limitations和blocked reason。
- 不回legacy adapter name、collector import path、endpoint、credentials、
  retention internal或operator command。

### `query_market_data`

Input：

- `capability_id`：必須存在於manifest且獲host allowlist授權。
- `query_kind`：`metrics|supply|events|geographies|health`。
- `filters`：只接受capability template與現有`ReadQuery` allowlist交集。
- Optional RFC3339 UTC `as_of`、`cursor_ref`。
- `limit`：1至20，default 20。

`blocked` capability拒絕；`partial` capability保留manifest limitations。Facade先把
capability template收窄成`ReadQuery`，model不能以filters繞過datasource／metric
scope。`as_of` 只接受完整
`YYYY-MM-DDTHH:MM:SS[.1-6 digits]Z`：先 full regex，再以 UTC `fromisoformat`
驗證 calendar；date-only、offset、lowercase `z`、空白 separator、leap second 及
超過六位 fraction 全部拒絕。

Output保留：

- Canonical anchor、observation／datasource ID。
- Normalized payload、unit、definition、period、source date和retrieved time。
- 對manifest明列的numeric capability，Facade另投影host-derived `numeric` object：
  `value`、`unit`、`definition`、`as_of`、nullable `source_date`和
  `period_label`。`value`只可從manifest的fixed direct field取得；Bank Rate必須取
  實際canonical payload的`bank_rate_percent`，不可假設generic `payload.value`。
- Evidence IDs、exact `citation_ref` values、retrieval／observation freshness、
  degraded和canonical availability。
- `cursor_ref`和warnings。

Phase 1新增access-aware `citation_projection_v1`。Facade用
`query_data_v1`回傳的fixed anchor及observations重新解析該anchor實際選中的
canonical run、evidence和locator，再mint `citation_ref`；不得只以observation ID
查「目前最新」evidence。

Facade serialized result上限256 KiB。超限時找出可完整emit的最大record prefix，
用第一個response的fixed anchor重新以該prefix limit執行`query_data_v1`，並以
「最後一筆實際emit」的boundary mint `cursor_ref`。返回`partial`和warning；多頁
不得skip或duplicate record，也不可靜默丟records。若第一筆完整record已超過
256 KiB，返回`RESULT_TOO_LARGE`且不mint cursor；不得返回zero-progress partial。

`result_ref`不屬於v1 model input。Non-promoted result只回job狀態；Agent必須在
production promotion後重新query canonical。

### `get_citation_metadata`

Input只接受本scope先前`query_market_data`回傳的`citation_ref`，最多20個。它不
接受裸observation／evidence ID、free-text query或artifact path。Projection依ref
內的anchor、canonical run、observation、evidence和locator hash重建**原查詢當時**
的lineage。

每個citation object所有keys固定存在：

- 必填非null：`citation_ref`、`observation_id`、`evidence_id`、
  `datasource_id`、`publisher`、`retrieved_at`、`access_class`、
  `data_kind`、`confidence`、`limitations[]`和bounded `locator`。
- Nullable：`title`、allowlisted／sanitized `public_url`、`published_at`、
  `source_updated_at`和`licence_or_attribution`。

不回raw bytes、CAS path、artifact URI、request headers/body、full PDF／HTML／ZIP
或excerpt。Nullable user-facing metadata缺失時返回`partial`和field-level warning；
identity／locator缺失則該citation不能用於fact。Access denied不洩漏restricted
artifact是否存在。

### `request_data_refresh`

Model input只含：

- `capability_id`。
- `datasource_id`。
- `request_profile`。
- `bounded_scope`。
- `intent`。

Agent不能提供endpoint、URL、lane、definition version、promotion、retention
deadline、`refresh_request_id`或confirmation token。Agent-facing result固定為：

- `disposition: accepted|deduplicated|already_fresh|approval_required`。
- Nullable `job_ref`、`approval_id`、`approval_expires_at`、`canonical_anchor`和
  `poll_after_seconds`，以及`initial_state`、`submitted_at`。
- `accepted`／`deduplicated`必須有scope-bound `job_ref`及正整數
  `poll_after_seconds`；`already_fresh`只回canonical anchor；
  `approval_required`只回opaque `approval_id`和RFC3339 UTC
  `approval_expires_at`。

`poll_after_seconds`是該job在此session內每兩次status call之間的minimum cadence，
不只限制第一次poll；Runtime在每次pending response後以同一duration重設timer。

Request只enqueue durable job，不同步回傳市場數據。

Facade提供packaged profile factory，v1固定：

| Profile | Datasource | Scope | Facade freshness precheck |
|---|---|---|---|
| `bank-rate-latest` | `boe.bank_rate.iudbedr` | fixed latest series | canonical health／latest |
| `onspd-one-postcode` | `ons.onspd.postcode` | exactly one normalized postcode | none；直接交broker |

Model只能選host及capability manifest共同allowlist的profile。
`refresh_request_id`由host生成並映射到
`RefreshContext.request_instance_id`。Facade只對profile明列的safe selector做
precheck；Phase 1的`already_fresh`承諾只適用Bank Rate。ONSPD目前沒有safe
postcode read selector，因此hidden test直接交broker，不能由Facade聲稱fresh。
Production backend只需承諾其實際產生的`accepted`／`deduplicated`／
`confirmation_required`；Facade把最後一項投影為`approval_required`並移除token。

### `get_refresh_status`

Input只含`job_ref`。每次call立即返回，不在Facade wait：

- job state和latest attempt。
- retry／safe terminal error。
- promotion status和`canonical_changed`。

`canonical_changed`表示此job的terminal approved promotion在完成時是否改變
canonical selection，不表示該run永遠是current latest；未terminal時為null。
Status不回observation／evidence IDs、health payload或raw `result_ref`。Terminal
production job後，Agent必須以`query_market_data`另查canonical／health；
`succeeded`不等於已promotion。

## ONSPD human approval

每日第21個新ONSPD refresh沿用已接受的`confirmation_required`規則，但token
不是Agent capability。Facade只擁有host／model boundary：

1. Agent-facing result只回`approval_required`和opaque `approval_id`。
2. Existing data-plane `refresh_confirmation`保留token。新增immutable approval
   request mapping，保存`approval_id`、principal、scope、`refresh_request_id`、
   capability ID、manifest version、profile version、request fingerprint、
   issued／expiry，
   以及schema-validated canonical JSON snapshot：`datasource_id`、
   `request_profile`、normalized `bounded_scope`和`intent`；不複製token。
3. Decision／replay outcome寫入獨立append-only approval event；同一approval只允許
   一個approve或deny decision，相同decision retry必須idempotent，矛盾decision拒絕。
4. Host-only `approve_refresh(approval_id, decision)`先驗證session／principal／scope、
   expiry、manifest／profile version和snapshot fingerprint。批准時從trusted data
   plane按`refresh_request_id`解析既有token，從snapshot重建`RefreshRequest`，再以
   同一ID建立`RefreshContext.request_instance_id`並重送；拒絕時不呼叫broker。
   Operation可在Facade subprocess exit／restart後重試，靠data-plane request
   idempotency收斂。
5. 它不註冊為Pi Tool，也不接受新的scope、intent、profile或request payload。
   Expired、changed scope／principal／policy或矛盾decision均拒絕。

UI notification、HTTP approval endpoint和follow-up turn由第二階段Runtime Decision
擁有。Model不能看見、儲存或提交confirmation token。

## Capability coverage

第一個Runtime allowlist只授權`uk.bank-rate-current`查詢capability及
`bank-rate-latest` refresh profile。ONSPD profile在Phase 2只做hidden approval
integration test，待`resolve-london-geography` rollout才加入launch allowlist。
Facade可描述其他operational sources，但不能把以下範圍標為完整產品能力：

- Prime rent和City／West End vacancy。
- Ranked market news。
- Project-level development／refurbishment／pre-let pipeline。
- Broker submarket comparison。
- Investment transactions、flight-to-quality或vacancy anomaly。

Blocked、manual-review和discovery data不因Agent request或Skill wording而變成
canonical production coverage。

## Verification and exit gate

實作完成必須通過：

1. Shared valid／invalid JSON fixtures、64-KiB input、256-KiB output、unknown fields
   及stable error mapping。
2. Product capability manifest schema／policy enforcement；blocked coverage不能query。
3. Canonical latest、as-of、access class、scoped cursor tampering和filter bounds；
   Bank Rate numeric projection從`bank_rate_percent`產生exact decimal string。
4. 20-record／256-KiB bounds；size-truncated多頁沒有skip／duplicate；單筆超限回
   `RESULT_TOO_LARGE`且不產生zero-progress cursor。
5. Exact citation ref保留anchor／canonical run lineage；source/date/locator、
   nullable metadata、missing metadata和restricted access受測。
6. Refresh accepted、deduplicated、Facade already-fresh、invalid scope、denied、
   retryable failure和terminal promotion；三次status poll遵守每次minimum cadence，
   不可burst。
7. Packaged Bank Rate／ONSPD profiles與host-context logical
   `refresh_request_id`；同turn兩個refresh不collision，retry沿用同一ID。
8. URL、lane、promotion、principal、scope和access injection fail closed。
9. Cursor／citation／job／approval handle不可跨session、kind或expiry重播。
10. Source failure保留last-good canonical並回stale／degraded。
11. ONSPD token不出現在stdout、stderr、logs或model result；normalized snapshot
    能在Facade subprocess exit／restart後重建exact request，host-only approval仍
    驗證identity、scope、fingerprint、policy version、expiry及one-decision
    semantics。
12. Child timeout、cancel、crash、malformed或oversized stdout安全終止process group
    並轉成typed error。
13. Clean wheel install可使用獨立Agent binary和packaged manifests／profiles，
    operator commands不可達。
14. 完整offline datasource suite繼續通過。
15. Draft 2020-12 selector catalog、catalog schema、generic request/result schema、
    language-neutral valid/invalid/policy-invalid/partial fixtures，以及 actual Facade
    projection 共同驗證；success/error conditional 和 `_ERROR_DETAILS` parity 不可漂移。
16. Strict `as_of` regex/calendar tests涵蓋 valid whole/fractional UTC、date-only、
    offset、lowercase、separator、leap second、invalid calendar date 和超長 fraction。
17. 每個 selector constructor isolation 受測；三個 read selector 沒有 writer/broker
    dependencies，status selector 沒有 read repository。
18. Same scope close/reopen、concurrent duplicate、generated scope uniqueness 和 process
    restart handle invalidation 受測；同一 process 不能重用已消耗 scope。

Exit gate是：非Python consumer只依版本化schemas和fixtures即可安全整合，且
registered Agent tool surface無法取得operator、raw evidence、collector、
non-canonical result或canonical writer capability。

## 實作證據（2026-08-02）

- `nan_fung.agent_tools` 現有 strict UTF-8 JSON wire parser/serializer、stable
  error/exit mapping、`AgentToolFacade`、runtime-scoped HMAC handles、packaged
  capability/profile/contract loaders、`AgentToolHost`/`AgentToolSession` 及獨立
  `nan-fung-agent-tools <tool-name>` binary。Host 以 inherited FD 3 傳遞每個 runtime
  boot 的 256-bit key；launcher 使用 `shell=False`、獨立 process group、10-second
  timeout/cancel 和 bounded stdin/stdout/stderr。
- 新增 Draft 2020-12 `agent_tool_contracts.v1` catalog、catalog schema、Python loader
  及 language-neutral valid/invalid/policy-invalid/partial fixtures；generic result
  conditional/error parity 與 selector success projections 均由 `jsonschema` tests
  驗證。所有 catalog/schema JSON assets 已包含在 wheel。
- `as_of` 已收窄為完整 UTC RFC3339 calendar instant；`_runtime_facade(tool_name)`
  實作 selector-minimal dependencies，三個 canonical reads 不建 writer/broker；
  `AgentToolHost` 對已使用 scope 保留 lock-protected process-lifetime tombstone，
  close 後或 concurrent replay 均不可重用。
- Bank Rate launch capability 固定為 canonical `metrics`、
  `boe.bank_rate.iudbedr`、`bank_rate_percent` decimal-string、UK scope；
  `uk.postcode-resolution` 維持 query-disabled partial approval integration，其他
  規劃外 coverage 維持 blocked。
- Phase 1 closure commands 全數通過：

  ```bash
  uv run pytest -q tests/test_agent_tool_contracts.py \
    tests/test_agent_tool_cli_dependencies.py \
    tests/test_agent_tool_host_scopes.py \
    tests/test_agent_tool_capabilities.py \
    tests/test_agent_tool_query_citations.py \
    tests/test_agent_tool_process.py \
    tests/test_agent_tool_refresh.py
  # 69 passed
  uv run pytest -q
  # 381 passed, 15 deselected
  uv run python -m compileall -q src
  git diff --check
  ```

- `uv build --wheel` 成功；temporary clean venv 安裝 wheel（含正常 runtime
  dependencies）後確認 contract/schema assets 可由 `importlib.resources` 讀取，
  `load_tool_contracts()` 回傳六個 selectors，且 inherited-FD harness 成功呼叫
  `nan-fung-agent-tools describe_market_data`。selector constructor isolation test
  另確認 read selector 不建立 writer/broker dependencies。

以上 exit gates 均通過，因此 `implementation_status: complete` 維持成立；Phase 2
仍依其 2a／2b／2c mandatory gates 另行驗收。

## References

- [[wiki/research/agent-skill-and-tool/skill-and-tool-design|Agent Runtime, Skill and Tool Research]]
- [[wiki/architecture/agent-runtime|Agent Runtime Architecture]]
- [[wiki/architecture/data-access-freshness|Data Access and Freshness Architecture]]
- [[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status]]
- [Datasource operations](../../docs/datasource-operations.md)
- [Datasource acceptance](../../docs/datasource-acceptance.md)
