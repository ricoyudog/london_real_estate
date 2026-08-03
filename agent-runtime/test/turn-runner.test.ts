import assert from "node:assert/strict";
import test from "node:test";

import type { BootedRuntime } from "../src/boot.ts";
import type { ToolResult } from "../src/facade-launcher.ts";
import { BudgetExceeded, TurnCancelled, TurnContext, TurnDeadlineExceeded, defaultTurnLimits, type SessionContext } from "../src/runtime.ts";
import { cancelTurn, resumeTurn, runTurn } from "../src/turn-runner.ts";

const context: SessionContext = { principal: "principal", capability_scope_id: "scope", allowed_access_classes: ["open"], allowed_capability_ids: ["capability"], allowed_refresh_profiles: ["profile"] };

test("(a) 9th facade call is rejected", async () => { const runtime = fake(async (driver) => { for (let index = 0; index < 9; index += 1) await driver.call("describe_market_data", {}); }); await run(runtime); assert.equal(runtime.results.at(-1)?.error?.code, "BUDGET_EXCEEDED"); });
test("(b) 4th status poll is rejected", async () => { const runtime = fake(async (driver) => { for (let index = 0; index < 4; index += 1) await driver.call("get_refresh_status", { job_ref: `${index}` }); }); await run(runtime); assert.equal(runtime.results.at(-1)?.error?.code, "BUDGET_EXCEEDED"); });
test("(c) 3rd unsuccessful finalization is rejected", async () => { const runtime = fake(async (driver) => { for (let index = 0; index < 3; index += 1) await driver.call("finalize_market_brief", {}); }, [failed(), failed()]); await run(runtime); assert.equal(runtime.results.at(-1)?.error?.code, "BUDGET_EXCEEDED"); });
test("(d) 21st query item is rejected", async () => { const runtime = fake(async (driver) => { await driver.call("query_market_data", { limit: 21 }); }); await run(runtime); assert.equal(runtime.results[0]?.error?.code, "BUDGET_EXCEEDED"); });
test("(e) 41st cumulative record is rejected", async () => { const runtime = fake(async (driver) => { await driver.call("query_market_data", { limit: 20 }); await driver.call("query_market_data", { limit: 20 }); await driver.call("describe_market_data", {}); }, [ok({ records: records(20) }), ok({ records: records(20) }), ok({ records: records(1) })]); await run(runtime); assert.equal(runtime.results.at(-1)?.error?.code, "BUDGET_EXCEEDED"); });
test("(f) 128 KiB plus one model bytes is rejected", async () => { const runtime = fake(async (driver) => { await driver.call("describe_market_data", {}); }, [ok({ blob: "x".repeat(defaultTurnLimits.cumulativeModelToolBytes) })]); await run(runtime); assert.equal(runtime.results[0]?.error?.code, "BUDGET_EXCEEDED"); });
test("(g) polling cadence correlates requested job_ref even when status results omit it", async () => { const runtime = fake(async (driver) => { await driver.call("request_data_refresh", {}); await driver.call("get_refresh_status", { job_ref: "job" }); runtime.advance(15_000); await driver.call("get_refresh_status", { job_ref: "job" }); await driver.call("get_refresh_status", { job_ref: "job" }); }, [ok({ job_ref: "job", poll_after_seconds: 15 }), ok({ job_state: "running" })]); await run(runtime); assert.equal(runtime.results[1]?.error?.code, "POLICY_DENIED"); assert.equal(runtime.results[2]?.status, "ok"); assert.equal(runtime.results[3]?.error?.code, "POLICY_DENIED"); });
test("(h) deadline is surfaced as a typed error", async () => { const runtime = fake(async (driver) => { runtime.advance(defaultTurnLimits.turnDeadlineMs); await driver.call("describe_market_data", {}); }); await run(runtime); assert.equal(runtime.results[0]?.error?.code, "TURN_DEADLINE_EXCEEDED"); });
test("(i) terminal refresh correlates the requested job when the result omits job_ref", async () => { const runtime = fake(async (driver) => { await driver.call("get_refresh_status", { job_ref: "job" }); await driver.call("finalize_market_brief", {}); await driver.call("query_market_data", { limit: 1 }); await driver.call("finalize_market_brief", {}); }, [ok({ job_state: "succeeded", canonical_changed: true }), ok({ records: [] })]); await run(runtime); assert.equal(runtime.results[1]?.error?.code, "REQUERY_REQUIRED"); assert.equal(runtime.results[3]?.status, "ok"); });
test("(j) cancel retains durable refresh and produces cancelled", async () => { const runtime = fake(async (driver) => { await driver.call("request_data_refresh", {}); cancelTurn(runtime.booted); }); const outcome = await run(runtime); assert.equal(outcome.terminal_state, "cancelled"); assert.equal(outcome.artifact, undefined); assert.equal(runtime.calls[0], "request_data_refresh"); });
test("(k) a time-free request always reaches Pi instead of host clarification", async () => { const runtime = fake(async () => undefined); const outcome = await runTurn(runtime.booted, "倫敦金融城本季 Prime office rent 是多少？", { now: runtime.now }); assert.equal(runtime.prompts, 1); assert.equal(outcome.clarification_requested, undefined); });
test("(l) explicit latest requests still prompt Pi", async () => { const runtime = fake(async () => undefined); await run(runtime); assert.equal(runtime.prompts, 1); });
test("(m) continuation reuses its original context and deadline", async () => { const runtime = fake(async () => undefined); const outcome = await run(runtime); const turn = outcome.turn; runtime.advance(1_000); const remaining = turn.getDeadlineRemainingMs(); const resumed = await resumeTurn(runtime.booted, outcome); assert.equal(resumed.turn, turn); assert.equal(turn.getDeadlineRemainingMs(), remaining); });
test("(m.1) a stalled Pi prompt reaches deadline and is aborted", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const runtime = fake(async () => undefined, [], { stallPromptUntilAbort: true });
  const pending = run(runtime);

  t.mock.timers.tick(defaultTurnLimits.turnDeadlineMs);
  await new Promise<void>((resolve) => setImmediate(resolve));

  assert.equal(runtime.aborts, 1);
  const outcome = await pending;
  assert.equal(outcome.terminal_state, "failed");
  assert.equal(outcome.reason_code, "TURN_DEADLINE_EXCEEDED");
  assert.deepEqual(outcome.events.map((event) => event.type), ["turn.started", "turn.failed"]);
  assert.equal(runtime.booted.getTurnContext(), undefined);
});
test("(m.2) cancelling a stalled Pi prompt aborts it before the deadline", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const runtime = fake(async () => undefined, [], { stallPromptUntilAbort: true });
  const pending = run(runtime);
  await new Promise<void>((resolve) => setImmediate(resolve));

  cancelTurn(runtime.booted);
  await new Promise<void>((resolve) => setImmediate(resolve));
  const abortsAfterCancel = runtime.aborts;

  t.mock.timers.tick(defaultTurnLimits.turnDeadlineMs);
  const outcome = await pending;
  assert.equal(abortsAfterCancel, 1);
  assert.equal(outcome.terminal_state, "cancelled");
  assert.equal(outcome.reason_code, undefined);
  assert.deepEqual(outcome.events.map((event) => event.type), ["turn.started", "turn.completed"]);
  assert.equal(runtime.booted.getTurnContext(), undefined);
});
test("(n) split numeric model text fails without an artifact", async () => { const runtime = fake(async (_driver, emit) => { emit(delta("4")); emit(delta(".5")); emit(delta("%")); }); const outcome = await run(runtime); assert.equal(outcome.terminal_state, "failed"); assert.equal(outcome.reason_code, "NUMERIC_GUARD_REJECTED"); assert.equal(outcome.artifact, undefined); });
test("(n.0a) a turn without a host-finalized artifact has a safe reason", async () => { const runtime = fake(async () => undefined); const outcome = await run(runtime); assert.equal(outcome.terminal_state, "failed"); assert.equal(outcome.reason_code, "NO_FINAL_ARTIFACT"); });
test("(n.0) host finalization supersedes earlier numeric model prose", async () => { const runtime = fake(async (driver, emit) => { emit(delta("4")); emit(delta(".5")); emit(delta("%")); await driver.call("finalize_market_brief", {}); }, [ok({ schema_version: "market_brief.v1" })]); const outcome = await run(runtime); assert.equal(outcome.terminal_state, "completed"); assert.deepEqual(outcome.artifact, { schema_version: "market_brief.v1" }); });
test("(n.1) host finalization ignores later model prose", async () => { const runtime = fake(async (driver, emit) => { await driver.call("finalize_market_brief", {}); emit(delta("1.")); }, [ok({ schema_version: "market_brief.v1" })]); const outcome = await run(runtime); assert.equal(outcome.terminal_state, "completed"); assert.deepEqual(outcome.artifact, { schema_version: "market_brief.v1" }); });
test("(n.2) host finalization rejects all later tool calls", async () => { const runtime = fake(async (driver) => { await driver.call("finalize_market_brief", {}); await driver.call("describe_market_data", {}); }, [ok({ schema_version: "market_brief.v1" })]); await run(runtime); assert.equal(runtime.results[1]?.error?.code, "BRIEF_FINALIZED"); });
test("(n.3) host finalization aborts the Pi session tool loop", async () => { const runtime = fake(async (driver) => { await driver.call("finalize_market_brief", {}); }, [ok({ schema_version: "market_brief.v1" })]); await run(runtime); assert.equal(runtime.aborts, 1); });
test("(t) query and citation ledger entries preserve one projection per citation", async () => {
  const runtime = fake(async (driver) => {
    await driver.call("query_market_data", {});
    await driver.call("get_citation_metadata", {});
  }, [
    ok({ anchor_as_of: "2026-08-02T00:00:00Z", records: [
      { observation_id: "observation-a", citation_refs: ["citation-a"], numeric: numeric("5.25") },
      { observation_id: "observation-b", citation_refs: ["citation-b"], numeric: numeric("6.50") },
    ] }),
    ok({ citations: [
      { observation_id: "observation-a", citation_ref: "citation-a", published_at: null, confidence: "high", publisher: "Publisher A", public_url: "https://example.test/a" },
      { observation_id: "observation-b", citation_ref: "citation-b", published_at: "2026-08-01", confidence: "medium", publisher: "Publisher B" },
    ] }),
  ]);

  const outcome = await runTurn(runtime.booted, "latest market data", { now: runtime.now, populateLedger: true });

  const queryEntries = outcome.turn.getLedger().filter((entry) => entry.kind === "query");
  const citationEntries = outcome.turn.getLedger().filter((entry) => entry.kind === "citation");
  assert.deepEqual(queryEntries.map((entry) => [entry.observation_ids, entry.citation_refs, projectionField(entry.numeric_projection, "value")]), [
    [["observation-a"], ["citation-a"], "5.25"],
    [["observation-b"], ["citation-b"], "6.50"],
  ]);
  assert.deepEqual(citationEntries.map((entry) => [entry.citation_refs, projectionField(entry.numeric_projection, "source")]), [
    [["citation-a"], "Publisher A"], [["citation-b"], "Publisher B"],
  ]);
  assert.equal(projectionField(citationEntries[0]?.numeric_projection, "public_url"), "https://example.test/a");
});

type Driver = { readonly call: (toolName: string, args: unknown) => Promise<void> };
type Policy = { readonly preToolCall?: (toolName: string, args: unknown, turn: TurnContext) => ToolResult | undefined; readonly onResult?: (toolName: string, args: unknown, result: ToolResult, turn: TurnContext) => void };
type FakeOptions = { readonly stallPromptUntilAbort?: boolean };
function fake(script: (driver: Driver, emit: (event: Readonly<Record<string, unknown>>) => void) => Promise<void>, responses: readonly ToolResult[] = [], options: FakeOptions = {}) {
  let time = 0; let active: TurnContext | undefined; let policies: Policy = {}; let prompts = 0; let aborts = 0; let resumePrompt: (() => void) | undefined; const calls: string[] = []; const results: ToolResult[] = []; const queue = [...responses]; const listeners = new Set<(event: Readonly<Record<string, unknown>>) => void>();
  const driver: Driver = { async call(toolName, args) { const turn = active; assert.ok(turn); const blocked = policies.preToolCall?.(toolName, args, turn); if (blocked !== undefined) { results.push(blocked); return; } try { turn.beforeToolCall(toolName, itemArgs(args)); const response = queue.shift() ?? ok({}); calls.push(toolName); policies.onResult?.(toolName, args, response, turn); results.push(response); } catch (error) { results.push(errorResult(error)); } } };
  const session = { subscribe(listener: (event: Readonly<Record<string, unknown>>) => void) { listeners.add(listener); return () => listeners.delete(listener); }, async prompt() { prompts += 1; if (options.stallPromptUntilAbort) { await new Promise<void>((resolve) => { resumePrompt = resolve; }); return; } await script(driver, (event) => { for (const listener of listeners) listener(event); }); }, async abort() { aborts += 1; resumePrompt?.(); } };
  const tools = ["describe_market_data", "query_market_data", "get_refresh_status", "request_data_refresh", "finalize_market_brief"].map((name) => ({ name }));
  const booted: BootedRuntime = { session, tools: tools as BootedRuntime["tools"], ctx: context, launcher: {} as BootedRuntime["launcher"], async finalizeBrief() { return {}; }, getTurnContext: () => active, setTurnContext: (turn) => { active = turn; }, setTurnPolicies: (next) => { policies = next; }, runtimeIdentity: { runtime_engine: "pi-agent-session", model: "faux/model" } };
  return { booted, now: () => time, advance: (milliseconds: number) => { time += milliseconds; }, calls, results, get prompts() { return prompts; }, get aborts() { return aborts; } };
}
async function run(runtime: ReturnType<typeof fake>) { return runTurn(runtime.booted, "latest market data", { now: runtime.now }); }
function records(count: number): readonly Readonly<Record<string, unknown>>[] { return Array.from({ length: count }, () => ({ citation_refs: ["ref"] })); }
function ok(data: Readonly<Record<string, unknown>>): ToolResult { return { schema_version: "agent_tool_result.v1", request_id: null, status: "ok", data, warnings: [], error: null }; }
function failed(): ToolResult { return { schema_version: "agent_tool_result.v1", request_id: null, status: "error", data: null, warnings: [], error: { code: "INTERNAL_ERROR", message: "failed", retryable: false } }; }
function delta(chunk: string): Readonly<Record<string, unknown>> { return { type: "message_update", assistantMessageEvent: { type: "text_delta", delta: chunk } }; }
function numeric(value: string): Readonly<Record<string, unknown>> { return { value, unit: "percent", definition: "Bank Rate", as_of: "2026-08-01", source_date: "2026-08-01" }; }
function projectionField(value: unknown, name: string): unknown { return typeof value === "object" && value !== null && !Array.isArray(value) ? Reflect.get(value, name) : undefined; }
function itemArgs(args: unknown): { readonly items?: number } { if (typeof args !== "object" || args === null || Array.isArray(args)) return {}; const record = args as Readonly<Record<string, unknown>>; return typeof record["limit"] === "number" ? { items: record["limit"] } : {}; }
function errorResult(error: unknown): ToolResult { const code = error instanceof BudgetExceeded ? "BUDGET_EXCEEDED" : error instanceof TurnDeadlineExceeded ? "TURN_DEADLINE_EXCEEDED" : error instanceof TurnCancelled ? "TURN_CANCELLED" : "INTERNAL_ERROR"; return { schema_version: "agent_tool_result.v1", request_id: null, status: "error", data: null, warnings: [], error: { code, message: code, retryable: false } }; }
