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

- A material date/period omitted by the user requires clarification before any
  data call; explicit `latest` uses canonical latest.
- Explicit historical/as-of requests query that canonical anchor and do not
  silently refresh it.
- Relative-time fixtures use fixed absolute start/end boundaries in
  `Europe/London`; tests must not depend on the machine's current date or time
  zone.
- A terminal refresh is not a market-data result: the runner re-queries
  canonical data and preserves last-good data with stale/degraded/partial
  warnings when refresh fails.

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
