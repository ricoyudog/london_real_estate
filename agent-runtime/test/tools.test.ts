import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import { FacadeLauncher, type ToolResult } from "../src/facade-launcher.ts";
import { DraftRejected } from "../src/finalizer.ts";
import { TurnContext, defaultTurnLimits, type SessionContext } from "../src/runtime.ts";
import { createSessionTools, modelVisibleBytes } from "../src/tools.ts";
import Schema from "typebox/schema";

const worktreeRoot = resolve(import.meta.dirname, "../..");
const fixtures = JSON.parse(
  readFileSync(resolve(worktreeRoot, "tests/fixtures/agent_tools/v1/tool-contract-fixtures.json"), "utf8"),
) as {
  readonly valid: Readonly<Record<string, { readonly arguments: unknown }>>;
  readonly invalid: readonly { readonly selector: string; readonly target: string; readonly value: unknown }[];
};

const okResult: ToolResult = {
  schema_version: "agent_tool_result.v1",
  request_id: "call_test",
  status: "ok",
  data: {},
  warnings: [],
  error: null,
};

type Invocation = { readonly toolName: string; readonly request: unknown };

class SpyLauncher extends FacadeLauncher {
  readonly calls: Invocation[] = [];
  readonly #result: ToolResult;

  constructor(result: ToolResult = okResult) {
    super({ creDataDir: mkdtempSync(join(tmpdir(), "tools-test-")) });
    this.#result = result;
  }

  override async invoke(toolName: string, request: unknown): Promise<ToolResult> {
    this.calls.push({ toolName, request });
    return this.#result;
  }
}

function session(capability_scope_id: string): SessionContext {
  return {
    principal: "principal",
    capability_scope_id,
    allowed_access_classes: ["open"],
    allowed_capability_ids: ["uk.bank-rate-current"],
    allowed_refresh_profiles: ["bank-rate-latest"],
  };
}

function factory(launcher: SpyLauncher, ctx = session("scope_a"), turn?: TurnContext) {
  const activeTurn = turn ?? new TurnContext(ctx, defaultTurnLimits, { idFactory: () => "fixed" });
  return createSessionTools({
    ctx,
    launcher,
    finalizeBrief: async (draft) => ({ draft }),
    getTurnContext: () => activeTurn,
  });
}

function toolNamed(tools: ReturnType<typeof createSessionTools>, name: string) {
  const tool = tools.find((candidate) => candidate.name === name);
  assert.ok(tool, `missing tool ${name}`);
  return tool;
}

async function invoke(tool: ReturnType<typeof toolNamed>, toolCallId: string, args: unknown): Promise<unknown> {
  return Reflect.apply(tool.execute, tool, [toolCallId, args, undefined, undefined, undefined]);
}

test("tools accept catalog argument examples and reject catalog argument violations", () => {
  // Given: the language-neutral fixture's arguments entries; result-only invalid entries have no argument mapping.
  const tools = factory(new SpyLauncher());

  // When: each selector schema validates its matching fixture arguments.
  for (const [selector, fixture] of Object.entries(fixtures.valid)) {
    if (selector === "approve_refresh") continue;
    assert.equal(Schema.Compile(toolNamed(tools, selector).parameters).Check(fixture.arguments), true, selector);
  }

  // Then: every invalid arguments entry for a model selector is rejected.
  for (const fixture of fixtures.invalid) {
    if (fixture.target !== "arguments" || fixture.selector === "approve_refresh") continue;
    assert.equal(Schema.Compile(toolNamed(tools, fixture.selector).parameters).Check(fixture.value), false, fixture.selector);
  }
  assert.equal(Schema.Compile(toolNamed(tools, "request_data_refresh").parameters).Check({}), false);
  assert.equal(Schema.Compile(toolNamed(tools, "get_refresh_status").parameters).Check({}), false);
});

test("tools bind facade requests to their captured session context", async () => {
  // Given: a factory with an active turn and a launcher spy.
  const launcher = new SpyLauncher();
  const tools = factory(launcher);

  // When: each facade-backed tool is invoked with its valid catalog arguments.
  for (const selector of [
    "describe_market_data",
    "query_market_data",
    "get_citation_metadata",
    "request_data_refresh",
    "get_refresh_status",
  ]) {
    const tool = toolNamed(tools, selector);
    await invoke(tool, `call_${selector}`, fixtures.valid[selector]?.arguments);
  }

  // Then: each request carries its selector and captured host scope, with refresh identity only on refresh.
  assert.deepEqual(launcher.calls.map((call) => call.toolName), [
    "describe_market_data",
    "query_market_data",
    "get_citation_metadata",
    "request_data_refresh",
    "get_refresh_status",
  ]);
  for (const call of launcher.calls) {
    const request = call.request as { readonly host_context: Readonly<Record<string, unknown>> };
    assert.equal(request.host_context.principal, "principal");
    assert.equal(request.host_context.capability_scope_id, "scope_a");
    assert.equal("refresh_request_id" in request.host_context, call.toolName === "request_data_refresh");
  }
});

test("adapters strip fields outside their selector contract", async () => {
  // Given: a query tool invoked through the runtime boundary with an extraneous field.
  const launcher = new SpyLauncher();
  const tools = factory(launcher);

  // When: the adapter constructs its facade request.
  await invoke(toolNamed(tools, "query_market_data"), "call_strip", {
    capability_id: "uk.bank-rate-current",
    query_kind: "metrics",
    injected: "discarded",
  });

  // Then: the uncontracted field never reaches the launcher request.
  const request = launcher.calls[0]?.request;
  assert.ok(isFacadeRequest(request));
  assert.equal("injected" in request.arguments, false);
});

test("facade adapters expose compact aliases and resolve them only in the host", async () => {
  // Given: a canonical query result with host-only opaque handles.
  const opaqueCitation = "h1.opaque-citation-handle";
  const opaqueCursor = "h1.opaque-cursor-handle";
  const typedResult: ToolResult = {
    ...okResult,
    data: { records: [{ citation_refs: [opaqueCitation] }], cursor_ref: opaqueCursor },
  };
  const launcher = new SpyLauncher(typedResult);
  let finalized: unknown;
  const ctx = session("scope_a");
  const tools = createSessionTools({
    ctx,
    launcher,
    finalizeBrief: async (draft) => { finalized = draft; return {}; },
    getTurnContext: () => new TurnContext(ctx, defaultTurnLimits),
  });

  // When: the model queries, resolves the short citation alias, then finalizes with it.
  const result = await invoke(toolNamed(tools, "query_market_data"), "call_visible", {
    capability_id: "uk.bank-rate-current",
    query_kind: "metrics",
  });
  await invoke(toolNamed(tools, "get_citation_metadata"), "call_citation", { citation_refs: ["citation_1"] });
  await invoke(toolNamed(tools, "finalize_market_brief"), "call_final", {
    title: "Brief",
    status: "complete",
    facts: [{ claim_id: "rate", kind: "numeric", confidence: "high", numeric_citation_ref: "citation_1" }],
    inferences: [],
    limitations: [],
  });

  // Then: the model never receives opaque handles, while the trusted calls receive the originals.
  assert.equal(toolText(result), JSON.stringify({ status: "ok", data: { records: [{ citation_refs: ["citation_1"] }], cursor_ref: "cursor_1" } }));
  const citationRequest = launcher.calls[1]?.request;
  assert.ok(isFacadeRequest(citationRequest));
  assert.deepEqual(citationRequest.arguments, { citation_refs: [opaqueCitation] });
  assert.deepEqual(finalized, {
    title: "Brief",
    status: "complete",
    facts: [{ claim_id: "rate", kind: "numeric", confidence: "high", numeric_citation_ref: opaqueCitation }],
    inferences: [],
    limitations: [],
  });
});

test("finalize routes only to its injected finalizer", async () => {
  // Given: a finalizer dependency and a launcher spy.
  const launcher = new SpyLauncher();
  let calls = 0;
  const tools = createSessionTools({
    ctx: session("scope_a"),
    launcher,
    finalizeBrief: async (draft) => {
      calls += 1;
      return { draft };
    },
    getTurnContext: () => new TurnContext(session("scope_a"), defaultTurnLimits),
  });
  const draft = {
    title: "Brief",
    status: "complete",
    facts: [],
    inferences: [],
    limitations: [],
  };

  // When: finalize receives a bounded draft.
  await invoke(toolNamed(tools, "finalize_market_brief"), "call_final", draft);

  // Then: it invokes the finalizer and never invokes the facade launcher.
  assert.equal(calls, 1);
  assert.equal(launcher.calls.length, 0);
});

test("finalize exposes safe recovery guidance for a rejected unavailable draft", async () => {
  // Given: the host rejects a draft that claimed a fact without a resolved citation.
  const ctx = session("scope_a");
  const tools = createSessionTools({
    ctx,
    launcher: new SpyLauncher(),
    finalizeBrief: async () => { throw new DraftRejected("UNRESOLVED_REF"); },
    getTurnContext: () => new TurnContext(ctx, defaultTurnLimits),
  });

  // When: the model's finalizer call is rejected by the host boundary.
  const result = await invoke(toolNamed(tools, "finalize_market_brief"), "call_rejected", {
    title: "Unavailable",
    status: "unavailable",
    facts: [],
    inferences: [],
    limitations: ["Coverage is unavailable."],
  });

  // Then: it receives a bounded correction instead of a thrown tool exception.
  assert.equal(toolDetails(result).error?.code, "FINALIZE_REJECTED");
  assert.match(toolText(result), /empty facts and inferences/);
});

test("factory exposes exactly the six sequential model tools", () => {
  // Given: one session factory.
  const tools = factory(new SpyLauncher());

  // When: its tool definitions are inspected.
  const names = tools.map((tool) => tool.name).sort();

  // Then: only the decision-defined six sequential tools are present.
  assert.deepEqual(names, [
    "describe_market_data",
    "finalize_market_brief",
    "get_citation_metadata",
    "get_refresh_status",
    "query_market_data",
    "request_data_refresh",
  ]);
  assert.equal(names.includes("approve_refresh"), false);
  assert.ok(tools.every((tool) => tool.executionMode === "sequential"));
});

test("separate factories retain separate capability scope bindings", async () => {
  // Given: independent factory calls with different scope contexts.
  const firstLauncher = new SpyLauncher();
  const secondLauncher = new SpyLauncher();
  const first = factory(firstLauncher, session("scope_a"));
  const second = factory(secondLauncher, session("scope_b"));

  // When: both invoke describe_market_data.
  await invoke(toolNamed(first, "describe_market_data"), "call_a", {});
  await invoke(toolNamed(second, "describe_market_data"), "call_b", {});

  // Then: the hosts observe distinct captured capability scopes.
  const firstRequest = firstLauncher.calls[0]?.request as { readonly host_context: { readonly capability_scope_id: string } };
  const secondRequest = secondLauncher.calls[0]?.request as { readonly host_context: { readonly capability_scope_id: string } };
  assert.equal(firstRequest.host_context.capability_scope_id, "scope_a");
  assert.equal(secondRequest.host_context.capability_scope_id, "scope_b");
});

test("budget rejection returns a typed result before launcher invocation", async () => {
  // Given: an expired turn context.
  let now = 0;
  const ctx = session("scope_a");
  const expired = new TurnContext(ctx, defaultTurnLimits, { now: () => now });
  now = defaultTurnLimits.turnDeadlineMs;
  const launcher = new SpyLauncher();
  const tools = factory(launcher, ctx, expired);

  // When: a facade tool is attempted after its deadline.
  const result = await invoke(toolNamed(tools, "describe_market_data"), "call_expired", {});

  // Then: it reports a typed error and never reaches the launcher.
  assert.equal(launcher.calls.length, 0);
  assert.equal(toolDetails(result).status, "error");
  assert.equal(toolDetails(result).error?.code, "TURN_DEADLINE_EXCEEDED");
});

function toolDetails(result: unknown): ToolResult {
  assert.ok(isToolExecutionResult(result));
  return result.details;
}

function toolText(result: unknown): string {
  assert.ok(typeof result === "object" && result !== null && "content" in result);
  const content = result.content;
  assert.ok(Array.isArray(content) && content.length === 1);
  const item = content[0];
  assert.ok(typeof item === "object" && item !== null && "text" in item && typeof item.text === "string");
  return item.text;
}

function isToolExecutionResult(value: unknown): value is { readonly details: ToolResult } {
  if (typeof value !== "object" || value === null || !("details" in value)) return false;
  const details = value.details;
  return typeof details === "object" && details !== null && "status" in details;
}

function isFacadeRequest(value: unknown): value is { readonly arguments: Readonly<Record<string, unknown>> } {
  return typeof value === "object" && value !== null && "arguments" in value;
}

test("modelVisibleBytes counts only canonical model-visible fields", () => {
  // Given: a tool result containing a large warning and error detail.
  const result: ToolResult = { ...okResult, data: { ref: "opaque" }, warnings: ["x".repeat(1000)] };

  // When: model-visible bytes are measured.
  const bytes = modelVisibleBytes(result);

  // Then: only status and data contribute to the budget.
  assert.equal(bytes, Buffer.byteLength(JSON.stringify({ status: "ok", data: { ref: "opaque" } })));
});
