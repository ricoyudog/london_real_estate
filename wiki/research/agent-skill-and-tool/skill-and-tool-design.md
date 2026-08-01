---
type: wiki
updated: 2026-08-01
source: "[[wiki/architecture/agent-runtime|Agent Runtime Architecture: Pi + Python Data Plane]]"
tags: [agent, runtime, skills, tools, research]
---

# Agent Runtime, Skill and Tool Research

## 結論

`feat/datasource` tip `620b5c9` 已完成 canonical read、bounded refresh、
projection delivery 和 operator controls，並通過 `291 passed, 15 deselected`
的 offline suite。這使 Agent boundary 從假設收斂成可實作 contract：

- **Runtime** 使用 Pi 原生 `AgentSession`、custom tools、Skills 和 events；
  不另建 task manager、generic policy engine 或 child-agent framework。
- **Tool** 只能包裝 trusted Python read／refresh APIs；operator `cre`、raw
  datasource adapters、SQL、evidence bytes 和 promotion commands 不屬於 Agent
  capability。
- **Skill** 只保存 domain workflow、判讀規則和 coverage guardrails；不可再
  import Python datasource function、開 browser 或直接抓 upstream。

因此，舊方案「7 個 Skills 原樣保留、9 個 domain Tools 一次建立」不再成立。
新的交付分成兩階段：

1. [[wiki/decisions/agent-tool-facade-foundation|Agent Tool Facade Foundation]]
2. [[wiki/decisions/pi-agent-runtime-and-skills-vertical-slice|Pi Agent Runtime and Skills Vertical Slice]]

兩份 Decision 是正式規範；本頁保存研究證據、選項和推導。

### 開工判定

- **Phase 1：GO。** 可從JSON schemas、capability manifest、citation projection及
  failing contract tests開工。
- **Phase 2：GATED GO。** 設計已accepted，但不得與Phase 1平行整合；只有Phase 1
  exit gate全通過後才開始Pi Runtime和Skills vertical slice。

因此不把Runtime與Skills／Tools綁在同一次大改動，也不先做沒有真實Tool contract
的Runtime scaffold。

## 研究基線

### 已完成的 data plane

[[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource
Operational Implementation Status]] 已驗證：

- `query_data_v1` 提供 `metrics`、`supply`、`events`、
  `geographies` 和 `health` 五類 canonical latest／as-of reads。
- `request_refresh_v1`／`get_refresh_status_v1` 提供 allowlisted、
  asynchronous、durable refresh lifecycle。
- `ReadContext`／`RefreshContext` 由 trusted host 建立；Agent 不可選
  principal、access class、profile policy、lane 或 promotion。
- `cre` 是 local operator surface，不是 Agent boundary。
- Bank Rate、ONS／Nomis macro、VOA stock、ONS hybrid、MHCLG EPC 和 bounded
  ONSPD 已 operational；PLD、GOV.UK news、GLA、BNP 和 Rightmove 仍 blocked
  或 manual-review。

[Datasource Acceptance](../../../docs/datasource-acceptance.md) 同時確認：data
foundation engineering-complete 不等於 prime rent、vacancy、news、
project-level supply 或 submarket comparison 已具產品 coverage。

### Pi 官方能力

截至 2026-08-01，官方 package manifest 是
`@earendil-works/pi-coding-agent@0.83.0`，要求 Node `>=22.19.0`；同日
`npm view @earendil-works/pi-coding-agent version engines --json` 已確認published
registry提供該版本：

- [Pi package manifest](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json)
- [Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)
- [Pi Skills](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md)
- [Pi RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)

SDK 已原生提供：

- `createAgentSession()`、`SessionManager.inMemory()`、event subscription
  和 abort。
- `defineTool()`／`customTools` 及 built-in tool allowlist。
- `DefaultResourceLoader.skillsOverride`，可只載入 repository 內受信任的
  Skills。
- Skills 的 progressive disclosure：啟動時只放 name／description，命中時
  再讀完整 `SKILL.md`。

Node application 應直接嵌入 `AgentSession`；RPC 是非 Node host 的備選，
不是本產品主要 transport。

## Agent Runtime 研究

### 採用

- 一個 top-level Market Analyst Agent。
- Process-lifetime in-memory session；每個 session 只有一個 active turn。
- SSE 投影 Pi events；prompt、cancel 和 approval 使用普通 HTTP。
- Model 由明確 `PI_MODEL=<provider/model>` 設定；缺失或不可用時 fail
  startup，不讓 runtime 任意 fallback。
- 不直接開unrestricted built-ins；以Pi tool factories配rooted filesystem
  operations提供`read`、`grep`、`find`、`ls`，不開
  `bash`、`edit`、`write`。
- 以explicit Skill allowlist**取代**discovery；同時關閉extensions、packages、
  context files和prompt templates，resource root不含DB、CAS、config或credentials。

### 不採用

- Runtime-first：沒有 tool wire contract 時只會建立 mock boundary，後續重做。
- Skill-first：現有 Skills 會直接繞過 capture-before-parse、retention 和
  promotion。
- Pi RPC 作 Node 主邊界：SDK 已提供直接 embedding。
- custom `before_agent_start`、competition profile、通用 task manager 或
  recursive delegation：Competition MVP 沒有具體需求。

## Agent Tool 研究

### v1 Tool surface

| Tool | Responsibility | Backend |
|---|---|---|
| `describe_market_data` | 回傳 safe capability、coverage、metric／datasource IDs 和 limitations | registry／capability projection |
| `query_market_data` | bounded canonical metrics／supply／events／geographies／health query | `query_data_v1` |
| `get_citation_metadata` | scoped citation refs → safe source／date／locator／confidence metadata | 新增 access-aware citation projection |
| `request_data_refresh` | 申請 allowlisted durable refresh | `request_refresh_v1` |
| `get_refresh_status` | 讀 job、attempt、promotion和canonical change；health另由`query_market_data`查詢 | `get_refresh_status_v1` |

### 需要移除或延期的舊 Tool

| 舊 Tool | 新決定 |
|---|---|
| `query_market_metrics`／`get_data_health` | 合併到 `query_market_data`；只有 eval 證明 generic schema 容易誤用才加 semantic alias |
| `search_evidence` | 移除；Read API明確不支援evidence search，改為exact anchor-scoped citation resolver |
| `search_market_news` | 延期；沒有 production canonical news feed |
| `compare_submarkets` | 延期；沒有 approved comparable City／West End series |
| `get_supply_pipeline` | 延期；VOA 是 stock，不是 project pipeline |
| `analyze_metric_series` | P1；有真實 trend／anomaly acceptance case 後才建立 |
| datasource `submit_artifact` | 移除；v1 brief 只由 runtime validation，不寫 canonical data plane |

### Missing contract

目前 `ReadRecord` 有 observation／evidence IDs、unit、definition、period 和
freshness，但numeric payload key依datasource而異；Bank Rate實際使用
`bank_rate_percent`。Product manifest因此必須固定direct field selector，由Facade
投影統一numeric object，Runtime／Skill不可猜generic `payload.value`。ReadRecord也
沒有完整 user-facing source URL、publisher、publication／update
time、locator、confidence 和 limitations。正式 grounded answer 前必須補
`citation_projection_v1`及`get_citation_metadata`。因canonical evidence以
`canonical_run_id + observation_id`綁定，resolver必須從原query anchor產生
scope-bound `citation_ref`，不可只用observation ID重新查最新evidence；它不提供
raw artifact、CAS path、binary或excerpt。

Agent Tool Facade 另設比 data-plane contract 更窄的 model bounds：

- 每次 query 最多 20 records。
- 每次 citation resolve 最多 20 refs。
- Serialized result 最大 256 KiB，超限時 deterministic truncate 並保留
  最後一筆實際emit的scoped cursor／warning，不能skip records。
- `result_ref` v1 保持 host-internal；Agent 只在 production promotion 後
  re-query canonical。
- Product coverage由packaged versioned capability manifest執行，不從engineering
  operational status或Markdown推導。

## Agent Skill 研究

現有七個 Skills 沒有一個可原樣載入：

| Skill | 決定 |
|---|---|
| `track-uk-macro` | 重寫後首批啟用；只使用 canonical Bank Rate／ONS／Nomis |
| `check-office-esg` | 第二批重寫；只使用 MHCLG EPC proxy |
| `assess-office-demand` | 改為 `assess-hybrid-working-signal`，只處理 ONS proxy |
| `map-london-submarkets` | 改為 `resolve-london-geography`；不把行政 geography 冒充 broker submarket |
| `collect-office-market-metrics` | 拆出 `track-office-stock`；broad rent／vacancy 能力停用 |
| `monitor-market-news` | 停用至 canonical event source 獲批 |
| `track-office-supply` | 停用至 PLD licence／retention／production workflow 獲批 |

首個 vertical slice 只載入：

1. 重寫後的 `track-uk-macro`。
2. 新增 `generate-grounded-market-brief`。

每個 Skill 保持精簡、verb-led hyphen-case，並只保存：

- 何時查詢／refresh／停止。
- Fact、inference、proxy 和 coverage 的判讀規則。
- 來源失敗、stale、partial、blocked 時的回覆方式。

Tool schema、datasource registry、JSON examples、runtime budgets 和測試程序不複製
到 `SKILL.md`；它們由 Decision、typed contracts 和 tests 擁有。

## First vertical slice

使用者問題：

> 「截至指定時間，英國 Bank Rate 是多少？它對倫敦辦公室市場可能代表甚麼？」

固定 capability flow：

1. Query Bank Rate health／canonical observation。
2. 明確要求 latest 且 stale／missing 時申請 bounded refresh。
3. Bounded polling；terminal 後重新 query canonical。
4. 用原query產生的scoped refs resolve exact citation metadata。
5. 分開 facts、inferences 和 limitations。
6. Quantitative fact使用structured numeric claim；inference不引入number words或
   comparative quantity，衍生計算待deterministic Tool。
7. Model提交帶`supporting_citation_refs`的`market_brief_draft.v1`；Runtime按
   turn ledger驗證exact lineage並產生`market_brief.v1`，再以 SSE 送給 UI。

這條路徑驗證 runtime、Tool、Skill 和 citation integration，但只完成 macro fact
及 bounded interpretation。缺少 approved rent／transaction inputs 時，brief
必須是 `partial`，不可宣稱完整完成 TC-05。

## Related

- [[wiki/decisions/agent-tool-facade-foundation|Agent Tool Facade Foundation]]
- [[wiki/decisions/pi-agent-runtime-and-skills-vertical-slice|Pi Agent Runtime and Skills Vertical Slice]]
- [[wiki/architecture/agent-runtime|Agent Runtime Architecture]]
- [[wiki/architecture/data-access-freshness|Data Access and Freshness Architecture]]
- [[wiki/decisions/datasource-operational-implementation-2026-08-01|Datasource Operational Implementation Status]]
