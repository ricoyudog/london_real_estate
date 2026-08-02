import type { BootedRuntime } from "./boot.ts";
import type { ToolResult } from "./facade-launcher.ts";
import { ModelTextBuffer, NumericGuardViolation } from "./finalizer.ts";
import { LifecycleReducer, type AgentEvent, TurnContext, defaultTurnLimits } from "./runtime.ts";
import { modelVisibleBytes } from "./tools.ts";

export type RunOptions = { readonly now?: () => number; readonly onTurnCreated?: (turn: TurnContext) => void; readonly populateLedger?: boolean };
export type TurnOutcome = {
  readonly turn: TurnContext;
  readonly terminal_state: "completed" | "cancelled" | "failed";
  readonly artifact?: unknown;
  readonly events: readonly AgentEvent[];
  readonly clarification_requested?: boolean;
};

type SessionEvent = Readonly<Record<string, unknown>>;
type Session = {
  readonly subscribe: (listener: (event: SessionEvent) => void) => (() => void) | void;
  readonly prompt: (message: string) => Promise<void>;
  readonly abort?: () => Promise<void>;
};

type RunnerState = {
  readonly reducer: LifecycleReducer;
  readonly buffer: ModelTextBuffer;
  readonly now: () => number;
  readonly pollNotBefore: Map<string, number>;
  readonly pollIntervals: Map<string, number>;
  readonly populateLedger: boolean;
  requeryRequired: boolean;
  artifact: unknown;
};

const states = new WeakMap<TurnContext, RunnerState>();
const timeAnchor = /\b(?:latest|current|today|yesterday|last\s+(?:month|week|year))\b|\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)?\b|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}\b/i;
const analysisQuestion = /\b(?:affect|impact|drive|influence|outlook|implication|why|how\s+(?:would|might|does|do|will|should|could))\b/i;

export const isAnalysisQuestion = (userMessage: string): boolean => analysisQuestion.test(userMessage);
export const requiresClarification = (userMessage: string): boolean => !timeAnchor.test(userMessage) && !isAnalysisQuestion(userMessage);

export function cancelTurn(booted: BootedRuntime): void {
  booted.getTurnContext()?.requestCancel();
}

export async function runTurn(booted: BootedRuntime, userMessage: string, options: RunOptions = {}): Promise<TurnOutcome> {
  const turn = new TurnContext(booted.ctx, defaultTurnLimits, options);
  options.onTurnCreated?.(turn);
  booted.setTurnContext(turn);
  const state = newState(options.now ?? performance.now.bind(performance), options.populateLedger ?? false);
  states.set(turn, state);
  const outcome = await execute(booted, turn, userMessage, state, true);
  booted.setTurnContext(undefined);
  return outcome;
}

export async function resumeTurn(booted: BootedRuntime, outcome: TurnOutcome): Promise<TurnOutcome> {
  if (!outcome.turn.isActive()) throw new TypeError("cannot resume an inactive turn");
  const state = states.get(outcome.turn);
  if (state === undefined) throw new TypeError("turn has no runner state");
  if (state.reducer.state() !== "running") return outcome;
  return execute(booted, outcome.turn, "", state, false);
}

async function execute(booted: BootedRuntime, turn: TurnContext, userMessage: string, state: RunnerState, fresh: boolean): Promise<TurnOutcome> {
  if (fresh) state.reducer.transition({ type: "turn.started" });
  booted.setTurnPolicies({
    preToolCall: (toolName, args, active) => beforeTool(state, toolName, args, active),
    onResult: (toolName, result, active) => afterTool(state, toolName, result, active),
  });
  if (fresh && requiresClarification(userMessage)) return finish(state, turn, true);
  const session = sessionOf(booted.session);
  let rejectText = false;
  const unsubscribe = session.subscribe((event) => {
    if (state.reducer.state() === "failed" || state.reducer.state() === "completed" || state.reducer.state() === "cancelled") return;
    switch (event["type"]) {
      case "tool_execution_start":
        if (typeof event["toolName"] === "string") state.reducer.transition({ type: "tool.started", tool: event["toolName"] });
        return;
      case "tool_execution_end":
        if (typeof event["toolName"] === "string") state.reducer.transition({ type: "tool.completed", tool: event["toolName"], ok: toolResultOk(event["result"]) });
        return;
      case "message_update": {
        const chunk = textChunk(event);
        if (chunk === undefined) return;
        try { state.buffer.append(chunk); } catch (error) { if (error instanceof NumericGuardViolation) rejectText = true; else throw error; }
        return;
      }
      default:
        return;
    }
  });
  try {
    if (turn.isCancelled()) await session.abort?.();
    else await session.prompt(userMessage);
  } finally {
    if (typeof unsubscribe === "function") unsubscribe();
  }
  if (turn.isCancelled()) {
    state.reducer.transition({ type: "turn.completed", terminal_state: "cancelled" });
    return outcome(state, turn, "cancelled");
  }
  if (rejectText || state.buffer.guardRejected) {
    state.reducer.transition({ type: "turn.failed" });
    return outcome(state, turn, "failed");
  }
  try { state.buffer.flush(); } catch (error) { if (error instanceof NumericGuardViolation) { state.reducer.transition({ type: "turn.failed" }); return outcome(state, turn, "failed"); } throw error; }
  if (state.artifact === undefined) { state.reducer.transition({ type: "turn.failed" }); return outcome(state, turn, "failed"); }
  state.reducer.transition({ type: "artifact.final" });
  state.reducer.transition({ type: "turn.completed", terminal_state: "completed" });
  return outcome(state, turn, "completed");
}

function newState(now: () => number, populateLedger: boolean): RunnerState {
  return { reducer: new LifecycleReducer(), buffer: new ModelTextBuffer(), now, pollNotBefore: new Map(), pollIntervals: new Map(), populateLedger, requeryRequired: false, artifact: undefined };
}

function beforeTool(state: RunnerState, toolName: string, args: unknown, _turn: TurnContext): ToolResult | undefined {
  if (toolName === "finalize_market_brief" && state.requeryRequired) return failure("REQUERY_REQUIRED", "re-query canonical market data before finalizing");
  if (toolName !== "get_refresh_status" || !isRecord(args)) return undefined;
  const jobRef = args["job_ref"];
  if (typeof jobRef === "string" && state.now() < (state.pollNotBefore.get(jobRef) ?? 0)) return failure("POLICY_DENIED", "refresh status polling is too frequent");
  return undefined;
}

function afterTool(state: RunnerState, toolName: string, result: ToolResult, turn: TurnContext): void {
  if (result.status === "error") return;
  const data = result.data;
  const records = recordArray(data?.["records"]);
  if (state.populateLedger && toolName === "query_market_data" && data !== null) addQueryLedger(turn, data, records);
  if (state.populateLedger && toolName === "get_citation_metadata" && data !== null) enrichQueryLedger(turn, data);
  const citations = records.reduce((total, record) => total + stringArray(record["citation_refs"]).length, 0);
  const items = toolName === "get_citation_metadata" ? stringArray(data?.["citation_refs"]).length : records.length;
  turn.chargeAccumulators({ items, records: records.length, citations, modelToolBytes: modelVisibleBytes(result) });
  if (toolName === "request_data_refresh" && data !== null) {
    const jobRef = data?.["job_ref"];
    const seconds = data?.["poll_after_seconds"];
    if (typeof jobRef === "string" && typeof seconds === "number" && seconds > 0) {
      const interval = seconds * 1000;
      state.pollIntervals.set(jobRef, interval);
      state.pollNotBefore.set(jobRef, state.now() + interval);
    }
  }
  if (toolName === "get_refresh_status") updateRefreshState(state, data);
  if (toolName === "query_market_data") state.requeryRequired = false;
  if (toolName === "finalize_market_brief") state.artifact = result.data;
}

function addQueryLedger(turn: TurnContext, data: Readonly<Record<string, unknown>>, records: readonly Readonly<Record<string, unknown>>[]): void {
  const anchor = data["anchor_as_of"];
  const first = records[0];
  if (typeof anchor !== "string" || first === undefined) return;
  const observationIds = records.map((record) => record["observation_id"]).filter((value): value is string => typeof value === "string");
  const citationRefs = records.flatMap((record) => stringArray(record["citation_refs"]));
  const numeric = isRecord(first["numeric"])
    ? { ...first["numeric"], period_label: first["numeric"]["period_label"] ?? first["numeric"]["source_date"] }
    : undefined;
  turn.addLedgerEntry({
    kind: "query", anchor_as_of: anchor, observation_ids: observationIds, citation_refs: citationRefs,
    ...(numeric === undefined ? {} : { numeric_projection: numeric }),
    freshness: { retrieval_freshness: first["retrieval_freshness"], observation_freshness: first["observation_freshness"] },
  });
}

function enrichQueryLedger(turn: TurnContext, data: Readonly<Record<string, unknown>>): void {
  const citations = recordArray(data["citations"]);
  const citation = citations[0];
  const query = [...turn.getLedger()].reverse().find((entry) => entry.kind === "query");
  if (citation === undefined || query === undefined || !isRecord(query.numeric_projection)) return;
  Object.assign(query.numeric_projection, {
    published_at: citation["published_at"] ?? null,
    datasource_confidence: citation["confidence"],
    source: citation["publisher"],
    anchor_as_of: query.anchor_as_of,
  });
  const known = new Set(query.citation_refs);
  const novel = citations.filter((item) => typeof item["citation_ref"] === "string" && !known.has(item["citation_ref"]));
  if (novel.length === 0) return;
  turn.addLedgerEntry({
    kind: "citation", anchor_as_of: query.anchor_as_of,
    observation_ids: novel.map((item) => item["observation_id"]).filter((value): value is string => typeof value === "string"),
    citation_refs: novel.map((item) => item["citation_ref"]).filter((value): value is string => typeof value === "string"),
  });
}

function updateRefreshState(state: RunnerState, data: Readonly<Record<string, unknown>> | null): void {
  if (data === null) return;
  const jobRef = data?.["job_ref"];
  if (typeof jobRef !== "string") return;
  const jobState = data["job_state"];
  if (["succeeded", "empty", "failed", "dead_letter", "cancelled"].includes(typeof jobState === "string" ? jobState : "")) {
    state.pollNotBefore.delete(jobRef); state.pollIntervals.delete(jobRef);
    if (data["canonical_changed"] === true || jobState === "succeeded") state.requeryRequired = true;
    return;
  }
  const interval = state.pollIntervals.get(jobRef);
  if (interval !== undefined) state.pollNotBefore.set(jobRef, state.now() + interval);
}

function finish(state: RunnerState, turn: TurnContext, clarification: boolean): TurnOutcome {
  state.artifact = { kind: "clarification", prompt: "Please specify the date or period you want to discuss." };
  state.reducer.transition({ type: "artifact.final" });
  state.reducer.transition({ type: "turn.completed", terminal_state: "completed" });
  return { turn, terminal_state: "completed", artifact: state.artifact, events: state.reducer.events(), ...(clarification ? { clarification_requested: true } : {}) };
}

function outcome(state: RunnerState, turn: TurnContext, terminal_state: TurnOutcome["terminal_state"]): TurnOutcome {
  return { turn, terminal_state, events: state.reducer.events(), ...(state.artifact === undefined ? {} : { artifact: state.artifact }) };
}

function sessionOf(value: unknown): Session { if (!isRecord(value) || typeof value["subscribe"] !== "function" || typeof value["prompt"] !== "function") throw new TypeError("booted session lacks the Pi session surface"); return value as Session; }
function textChunk(event: SessionEvent): string | undefined { const update = event["assistantMessageEvent"]; return isRecord(update) && update["type"] === "text_delta" && typeof update["delta"] === "string" ? update["delta"] : undefined; }
function toolResultOk(value: unknown): boolean { return isRecord(value) && value["details"] !== undefined ? toolResultOk(value["details"]) : !isRecord(value) || value["status"] !== "error"; }
function recordArray(value: unknown): readonly Readonly<Record<string, unknown>>[] { return Array.isArray(value) ? value.filter(isRecord) : []; }
function stringArray(value: unknown): readonly string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; }
function isRecord(value: unknown): value is Readonly<Record<string, unknown>> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function failure(code: string, message: string): ToolResult { return { schema_version: "agent_tool_result.v1", request_id: null, status: "error", data: null, warnings: [], error: { code, message, retryable: false } }; }
