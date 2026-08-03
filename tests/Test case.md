# London Office Market Agent — Test Cases

## Common acceptance criteria

Every case should verify that the agent:

- cites each material data source, publication date, and confidence level; when
  `published_at` is `null`, emits an explicit publication-date warning rather
  than inventing one;
- separates objective facts from AI inferences;
- keeps datasource confidence, fact confidence, and inference confidence as
  separate fields rather than treating one as evidence for another;
- labels missing, stale, or incompatible data instead of inventing values.

## Catalog scope and Phase 2 runtime mapping

This file remains the **product acceptance catalog**. It does not say that the
Phase 2 runtime supports all ten questions. The initial runtime is a Bank Rate
vertical slice; product coverage is determined by the versioned capability
manifest, not by a Skill or a test question.

| Product case | Phase 2 treatment |
|---|---|
| TC-01 | Blocked-coverage representative. The runtime may at most describe current coverage; it must not query/refresh, produce a number/citation, or imply Prime rent is available. |
| TC-02–TC-04 | Deferred product acceptance pending the corresponding canonical capabilities. |
| TC-05 | Executable as a **partial Bank Rate** vertical slice: grounded UK macro facts plus clearly qualified London office inference; it does not make rent/transaction data available. |
| TC-06–TC-08 | Deferred product acceptance pending canonical event, vacancy, ESG, and transaction coverage. |
| TC-09 | Opt-in composite smoke only. It cannot replace deterministic runtime gates or imply full daily-brief coverage. |
| TC-10 | Deferred product acceptance pending complete submarket canonical coverage. |

### Runtime-derived time fixtures

The Phase 2 deterministic runner derives the following rules from this catalog:

- An unqualified request uses the latest canonical view; the returned artifact
  still carries the authoritative `as_of` and freshness metadata.
- Explicit historical/as-of requests query that canonical anchor and do not
  silently refresh it.
- Relative-time fixtures use fixed absolute start/end boundaries in
  `Europe/London`; tests must not depend on the machine's current date or time
  zone.
- A terminal refresh is not a market-data result: the runner re-queries
  canonical data and preserves last-good data with stale/degraded/partial
  warnings when refresh fails.

## Full-stack delivery verification — 2026-08-03

本輪驗收以 deterministic gates、真實 GLM-5.2 及 Docker in-app browser 三層互相
對帳。Docker 中的 `5.25 percent` 是 packaged fixture，不是 live market claim。

| Gate | Result |
|---|---|
| Python offline suite | `387 passed, 15 deselected`；包含 time-safe submarket promotion 與 demo initializer/Compose contracts。 |
| Node unit/integration | `184` tests；`182 passed, 2 skipped, 0 failed`，nested fixtures 已納入 `npm test`；`npm run typecheck` 通過。 |
| Production dependency audit | clean `npm ci` 後 Pi minimatch 實際 resolve `brace-expansion@5.0.9`，nested vulnerable copy 不存在；`npm audit --omit=dev` 為 `0 vulnerabilities`。 |
| Browser regression | `11 passed`；涵蓋 session/reload cleanup、direct host-validated transcript answer、unsupported artifact fail-closed、seeded/empty/stale overview、suggested prompts、中文/英文/多行/4000 字、double submit、16-turn limit、failure-after-success、cancel/retry、SSE replay、responsive、keyboard、reduced motion 與 axe。 |
| Real model CLI | `npm run test:glm` 使用 private `.env` 的 `glm/GLM-5.2` 通過；沒有 `modelsOverride` 或 fake session factory；實際序列為 `describe_market_data → query_market_data → get_citation_metadata → finalize_market_brief`。 |
| Docker lifecycle | fresh `down -v → up --build --wait` 自動 seed；`down → up --wait` 回報 marker `verified` 且不重複 seed；non-demo mode 遇 marker fail closed。 |

真實 Docker browser turn 與 sanitized `pi_turn_trace.v1` 對帳如下：

| Browser case | Turn ID | Host result / trace |
|---|---|---|
| London office overview | `_v9mvVPAJcuAyrn0qlyIZQ` | UI `Partial`；只顯示 canonical Bank Rate，rent/vacancy/transactions 明列 unavailable；`describe_market_data → query_market_data → get_citation_metadata → finalize_market_brief`。 |
| Cancel | `j2oYm8y3wDU_I5tqvmS-Dg` | `terminal_state: cancelled`，`duration_ms: 282.173`；舊 brief 清除，session 立即可接受下一輪。 |
| Bank Rate post-fix screenshot | `0ULVxje2nFA4Q5F7TI-ucg` | UI transcript 直接顯示 `5.25 percent` 與 Bank of England source；右側為 `Complete`，包含 as-of/published/confidence/lineage；`describe_market_data → query_market_data → get_citation_metadata → finalize_market_brief`，`duration_ms: 28494.039`。 |
| West End post-fix screenshot | `PC6jM0YRfAOOO7FV4lra2w` | UI `Unavailable`、0 vacancy facts/sources、沒有模型生成的 vacancy number；`describe_market_data → finalize_market_brief`，`duration_ms: 13867.462`。 |

Browser console 的 warning/error 記錄為空；trace 不含 prompt、bearer 或 API key；
SSE/replay/recovery 與 DOM 不含 raw `h1.*` citation handle，而以 `Source 1` 保留 claim-to-source
lineage。最新四張真實 UI 畫面為 [runtime/fixture](../wiki/questions/Test_result/screenshots/dashboard-glm-runtime-coverage-2026-08-03.png)、
[direct answer](../wiki/questions/Test_result/screenshots/dashboard-glm-bank-rate-answer-2026-08-03.png)、
[lineage/source](../wiki/questions/Test_result/screenshots/dashboard-glm-lineage-source-2026-08-03.png) 與
[safe unavailable](../wiki/questions/Test_result/screenshots/dashboard-glm-west-end-unavailable-2026-08-03.png)。完整紀錄見
[TC-01 dashboard UI test](../wiki/questions/Test_result/TC-01-dashboard-ui-test-2026-08-02.md)
及 [runtime live test](../wiki/questions/Test_result/TC-01-runtime-live-test-2026-08-02.md)。上述四種狀態已嵌入 [Architecture and Demo deck](../docs/London-Market-Desk-Architecture-and-Demo-2026-08-02.pptx) 第 4 頁；全 deck render、overflow 與 template-fidelity checks 均通過。

## TC-01: Prime rent lookup (simple)

**Question**  
「倫敦金融城本季 Prime office rent 是多少？」

**Observable data**

- Search results and selected market-report sources.
- Rent value, unit (for example, GBP per sq ft), reporting quarter, and submarket.
- Source publication date and confidence level.

**Expected agent output**

- A concise current-rent answer.
- A small table with the current figure, quarter-on-quarter and year-on-year change where available.
- Source citations, data date, and any unit or scope caveat.

## TC-02: Vacancy-rate comparison (simple)

**Question**  
「比較 City 和 West End 最新空置率。」

**Observable data**

- Vacancy rate, available floor area, reporting period, and source for each submarket.
- Whether both figures use the same market definition and reporting period.

**Expected agent output**

- A comparison table for City and West End.
- A conclusion on which market has the higher vacancy rate and the recent direction of change.
- A warning if the figures are not directly comparable.

## TC-03: Recent market news (simple)

**Question**  
「過去 7 天倫敦辦公室市場有甚麼重要新聞？」

**Observable data**

- News title, publisher, publication date, URL, affected location, company, or asset.
- Deduplication and relevance ranking results.

**Expected agent output**

- Three to five ranked news summaries.
- For each item: the factual event, likely market relevance, source, and date.
- A clear statement when there are no material events.

## TC-04: Future supply pipeline (moderate)

**Question**  
「City 未來三年有多少新供應？」

**Observable data**

- Development name, address, area, expected completion date, and status.
- Status classification: new build, refurbishment, or pre-let.
- Annual aggregate supply and pre-let share.

**Expected agent output**

- A project-level pipeline table and annual supply summary.
- Identification of the supply peak and the proportion already pre-let.
- A list of sources and stated gaps in project coverage.

## TC-05: Interest-rate impact analysis (moderate)

**Question**  
「英國利率變動可能如何影響倫敦辦公室租金？」

**Observable data**

- Bank of England rate decisions and dates.
- GDP, inflation, and employment indicators.
- Recent rent and investment-transaction data used in the analysis.

**Expected agent output**

- A facts section listing the macroeconomic data.
- A separate AI-inference section explaining possible effects on financing costs, investment demand, and rents.
- Confidence and uncertainty statements; no claim of direct causation without evidence.

## TC-06: Material-event alert (moderate)

**Question**  
「本週是否出現值得即時提示的市場事件？」

**Observable data**

- Alert severity score and triggering threshold.
- Relevant large lease, investment transaction, policy change, or metric anomaly.
- Event date, affected submarket, and supporting sources.

**Expected agent output**

- If triggered: a short alert containing the event, why it matters, affected market, and follow-up items.
- If not triggered: an explicit no-alert result and the checks performed.

## TC-07: Vacancy anomaly detection (moderate)

**Question**  
「Canary Wharf 的空置率是否出現異常變化？」

**Observable data**

- Multi-period vacancy-rate time series.
- Period-on-period and year-on-year changes.
- Baseline, anomaly threshold, missing observations, and related news.

**Expected agent output**

- A small trend table or chart.
- Classification as normal or anomalous, with the size of the change.
- Possible drivers labelled as inference, with linked supporting news where available.

## TC-08: Flight-to-quality and ESG demand (complex analysis)

**Question**  
「Flight to Quality 和 ESG 要求是否正在推動 Grade A 辦公室需求？」

**Observable data**

- Grade A versus non-Grade A rent and vacancy differentials.
- Major leasing transactions and tenant statements.
- ESG or energy-efficiency certification data.

**Expected agent output**

- An evidence table separating market statistics, transactions, and qualitative evidence.
- A conclusion of supported, partly supported, or insufficient evidence.
- Clear separation of verified facts and AI interpretation.

## TC-09: Daily market brief (complex report)

**Question**  
「生成今天的倫敦辦公室市場日報。」

**Observable data**

- Current-day news, latest market metrics, significant transactions, and supply updates.
- Data freshness, source coverage, and unavailable data.

**Expected agent output**

- A one-page management brief with executive summary, key indicators, material events, risks, opportunities, and watch items.
- A machine-readable JSON version containing the same key facts and citations.
- Explicit labels for data that is older than the report date.

## TC-10: Quarterly executive submarket report (complex report)

**Question**  
「生成倫敦辦公室市場季度管理層報告，並比較 City、West End、Canary Wharf。」

**Observable data**

- Rent, vacancy, leasing volume, future supply, and major transactions for each submarket.
- Macro indicators, reporting periods, data coverage, and full source inventory.
- Calculation inputs used by comparison tables and charts.

**Expected agent output**

- A structured report containing an executive summary, submarket comparison, supply-and-demand analysis, macro context, risks, opportunities, and outlook scenarios.
- Comparison tables or charts covering the three submarkets.
- An appendix with sources, publication dates, confidence levels, data gaps, and a clear distinction between facts and AI inferences.
