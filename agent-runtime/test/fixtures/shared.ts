import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";

import { fauxAssistantMessage, fauxText, fauxToolCall, type FauxResponseStep } from "@earendil-works/pi-ai";

import { bootRuntime, type BootOptions, type BootedRuntime } from "../../src/boot.ts";
import { FacadeLauncher, type ToolResult } from "../../src/facade-launcher.ts";
import { finalizeBrief, type MarketBriefV1 } from "../../src/finalizer.ts";
import { defaultTurnLimits, type AgentEvent, type SessionContext, type TurnContext } from "../../src/runtime.ts";
import { runTurn, type TurnOutcome } from "../../src/turn-runner.ts";
import { createFauxModels, FAUX_MODEL_REF } from "../helpers/faux-models.ts";
import { fixtureAssets } from "./fixture-assets.ts";

const runtimeRoot = resolve(import.meta.dirname, "../..");
const worktreeRoot = resolve(runtimeRoot, "..");
const evidenceRoot = join(runtimeRoot, "test/.evidence/fixtures");

export const context: SessionContext = {
  principal: "fixture-agent",
  capability_scope_id: "fixture-scope",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current", "london-prime-rent"],
  allowed_refresh_profiles: ["bank-rate-latest"],
};

export type Call = {
  readonly toolName: string;
  readonly request: unknown;
  readonly result: ToolResult;
};

type RefreshScript = Readonly<Record<string, readonly ToolResult[]>>;

export class RecordingLauncher extends FacadeLauncher {
  readonly calls: Call[] = [];
  readonly #script: Map<string, ToolResult[]>;

  constructor(dataDir: string, script: RefreshScript = {}) {
    super({ creDataDir: dataDir, assetsDir: fixtureAssets(worktreeRoot) });
    this.#script = new Map(Object.entries(script).map(([name, results]) => [name, [...results]]));
  }

  override async invoke(toolName: string, request: unknown): Promise<ToolResult> {
    const queue = this.#script.get(toolName);
    const scripted = queue?.shift();
    const result = scripted ?? await super.invoke(toolName, request);
    this.calls.push({ toolName, request, result });
    return result;
  }
}

export type Fixture = {
  readonly booted: BootedRuntime;
  readonly launcher: RecordingLauncher;
  readonly outcome: TurnOutcome;
  readonly fauxCalls: number;
  readonly createSessionCalls: number;
};

export async function runFixture(options: {
  readonly fixture: string;
  readonly prompt: string;
  readonly responses: (launcher: RecordingLauncher, clock: FixtureClock) => readonly FauxResponseStep[];
  readonly refreshScript?: RefreshScript;
  readonly empty?: boolean;
  readonly stale?: boolean;
}): Promise<Fixture> {
  const dataDir = seedStore(options.fixture, options.empty ?? false, options.stale ?? false);
  process.env.PI_MODEL = FAUX_MODEL_REF;
  process.env.CRE_DATA_DIR = dataDir;
  process.env.PI_OFFLINE = "1";
  const launcher = new RecordingLauncher(dataDir, options.refreshScript);
  const clock = new FixtureClock();
  const faux = createFauxModels(options.responses(launcher, clock));
  const realFactory = await import("@earendil-works/pi-coding-agent");
  let createSessionCalls = 0;
  const createSession: NonNullable<BootOptions["createSession"]> = async (sessionOptions) => {
    createSessionCalls += 1;
    return realFactory.createAgentSession(sessionOptions);
  };
  const booted = await bootRuntime(context, {
    modelsOverride: faux.models,
    launcher,
    createSession,
    finalizeBrief: async (draft, turn) => finalizeFixture(draft, turn, launcher),
  });
  const outcome = await runTurn(booted, options.prompt, { now: clock.now });
  writeFixtureEvidence(options.fixture, outcome, launcher.calls);
  assertFixtureInvariants(outcome);
  return { booted, launcher, outcome, fauxCalls: faux.faux.state.callCount, createSessionCalls };
}

export class FixtureClock {
  #milliseconds = 0;
  readonly now = (): number => this.#milliseconds;
  advance(milliseconds: number): void { this.#milliseconds += milliseconds; }
}

export function tool(name: string, argumentsValue: Readonly<Record<string, unknown>>): FauxResponseStep {
  return fauxAssistantMessage(fauxToolCall(name, argumentsValue), { stopReason: "toolUse" });
}

export function toolAfterResult(
  name: string,
  argumentsValue: () => Readonly<Record<string, unknown>>,
  before?: () => void,
): FauxResponseStep {
  return (piContext) => {
    assert.ok(piContext.messages.some((message) => message.role === "toolResult"));
    before?.();
    return fauxAssistantMessage(fauxToolCall(name, argumentsValue()), { stopReason: "toolUse" });
  };
}

export const finishText = (): FauxResponseStep => fauxAssistantMessage(fauxText("Brief ready."));

export function ok(data: Readonly<Record<string, unknown>>): ToolResult {
  return { schema_version: "agent_tool_result.v1", request_id: null, status: "ok", data, warnings: [], error: null };
}

export function numericDraft(launcher: RecordingLauncher, status: "complete" | "partial", limitations: readonly string[] = []) {
  const refs = queryRecord(launcher)["citation_refs"];
  assert.ok(Array.isArray(refs) && typeof refs[0] === "string");
  const ref = refs[0];
  return {
    title: "Bank Rate brief",
    status,
    facts: [{ claim_id: "bank-rate", kind: "numeric", confidence: "high", numeric_citation_ref: ref }],
    inferences: [],
    limitations,
  };
}

export function citationArgs(launcher: RecordingLauncher): Readonly<Record<string, unknown>> {
  return { citation_refs: queryRecord(launcher).citation_refs };
}

export function assertCalls(fixture: Fixture, expected: readonly string[]): void {
  const calls = fixture.outcome.events.filter((event) => event.type === "tool.started").map((event) => event.tool);
  assert.deepEqual(calls, expected);
}

export function artifact(outcome: TurnOutcome): MarketBriefV1 {
  assert.ok(isRecord(outcome.artifact) && outcome.artifact["schema_version"] === "market_brief.v1");
  return outcome.artifact as MarketBriefV1;
}

function seedStore(fixture: string, empty: boolean, stale: boolean): string {
  const dataDir = join(mkdtempSync(join(tmpdir(), `pi-fixture-${fixture}-`)), "data");
  if (empty) {
    execFileSync("uv", ["run", "cre", "--data-dir", dataDir, "db", "migrate"], { cwd: worktreeRoot });
  } else {
    execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_bank_rate.py", dataDir, "5.25", ...(stale ? ["--stale"] : [])], { cwd: worktreeRoot });
  }
  return dataDir;
}

function finalizeFixture(draft: unknown, turn: TurnContext, launcher: RecordingLauncher): MarketBriefV1 | Readonly<Record<string, unknown>> {
  if (!isRecord(draft) || !Array.isArray(draft["facts"]) || draft["facts"].length === 0) {
    return unavailableArtifact(draft);
  }
  const record = queryRecord(launcher);
  const numeric = record["numeric"];
  const citation = citationRecord(launcher);
  const anchor = queryData(launcher)["anchor_as_of"];
  const observationId = record["observation_id"];
  const citationRefs = record["citation_refs"];
  assert.ok(isRecord(numeric));
  assert.ok(typeof anchor === "string");
  assert.ok(typeof observationId === "string");
  assert.ok(Array.isArray(citationRefs) && citationRefs.every((ref) => typeof ref === "string"));
  const periodLabel = numeric["period_label"] ?? numeric["source_date"];
  assert.ok(typeof periodLabel === "string");
  turn.addLedgerEntry({
    kind: "query",
    anchor_as_of: anchor,
    observation_ids: [observationId],
    citation_refs: citationRefs,
    numeric_projection: {
      ...numeric,
      period_label: periodLabel,
      published_at: citation.published_at,
      datasource_confidence: citation.confidence,
      source: citation.publisher,
      anchor_as_of: anchor,
    },
    freshness: { retrieval: record.retrieval_freshness, observation: record.observation_freshness },
  });
  const finalized = finalizeBrief({ schema_version: "market_brief_draft.v1", ...draft }, turn);
  const stale = record.degraded === true;
  return stale ? { ...finalized, freshness_warnings: ["Canonical Bank Rate is stale; last-good value retained."] } : finalized;
}

function unavailableArtifact(draft: unknown): Readonly<Record<string, unknown>> {
  assert.ok(isRecord(draft));
  const title = draft["title"];
  const status = draft["status"];
  const limitations = draft["limitations"];
  assert.equal(typeof title, "string");
  assert.equal(typeof status, "string");
  assert.ok(Array.isArray(limitations));
  return {
    schema_version: "market_brief.v1",
    title, status, facts: [], inferences: [], limitations,
    sources: [], lineage: {}, freshness_warnings: [], display_text: title,
  };
}

function queryData(launcher: RecordingLauncher): Readonly<Record<string, unknown>> {
  const data = launcher.calls.filter((call) => call.toolName === "query_market_data").at(-1)?.result.data;
  assert.ok(data);
  return data;
}

function queryRecord(launcher: RecordingLauncher): Readonly<Record<string, unknown>> {
  const records = queryData(launcher)["records"];
  assert.ok(Array.isArray(records) && isRecord(records[0]));
  return records[0];
}

function citationRecord(launcher: RecordingLauncher): Readonly<Record<string, unknown>> {
  const citations = launcher.calls.find((call) => call.toolName === "get_citation_metadata")?.result.data?.["citations"];
  assert.ok(Array.isArray(citations) && isRecord(citations[0]));
  return citations[0];
}

function assertFixtureInvariants(outcome: TurnOutcome): void {
  assert.equal(outcome.events[0]?.type, "turn.started");
  const terminals = outcome.events.filter((event) => event.type === "turn.completed" || event.type === "turn.failed");
  assert.equal(terminals.length, 1);
  assert.ok(outcome.turn.limits === defaultTurnLimits);
  assert.equal(outcome.terminal_state, "completed");
  const finalIndex = outcome.events.findIndex((event) => event.type === "artifact.final");
  const completedIndex = outcome.events.findIndex((event) => event.type === "turn.completed");
  assert.ok(finalIndex >= 0 && finalIndex < completedIndex);
}

function writeFixtureEvidence(fixture: string, outcome: TurnOutcome, calls: readonly Call[]): void {
  const directory = join(evidenceRoot, fixture);
  mkdirSync(directory, { recursive: true });
  const toolNames = outcome.events.filter((event): event is Extract<AgentEvent, { type: "tool.started" }> => event.type === "tool.started").map((event) => event.tool);
  const records = calls.flatMap((call) => call.toolName === "query_market_data" && Array.isArray(call.result.data?.["records"]) ? call.result.data["records"] : []);
  const citations = calls.flatMap((call) => call.toolName === "get_citation_metadata" && Array.isArray(call.result.data?.["citations"]) ? call.result.data["citations"] : []);
  const bytes = calls.reduce((total, call) => total + Buffer.byteLength(JSON.stringify({ status: call.result.status, data: call.result.data })), 0);
  assert.ok(toolNames.length <= outcome.turn.limits.facadeCallsPerTurn);
  assert.ok(records.length <= outcome.turn.limits.cumulativeRecords);
  assert.ok(citations.length <= outcome.turn.limits.cumulativeCitations);
  assert.ok(bytes <= outcome.turn.limits.cumulativeModelToolBytes);
  writeJson(join(directory, "tool-calls.json"), toolNames);
  writeJson(join(directory, "budget.json"), { facade_calls: toolNames.length, records: records.length, citations: citations.length, model_tool_bytes: bytes, limits: outcome.turn.limits });
  writeJson(join(directory, "artifact.json"), stableEvidence(outcome.artifact));
  writeJson(join(directory, "events.json"), outcome.events);
}

function writeJson(path: string, value: unknown): void { writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`); }
function isRecord(value: unknown): value is Readonly<Record<string, unknown>> { return typeof value === "object" && value !== null && !Array.isArray(value); }

function stableEvidence(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableEvidence);
  if (!isRecord(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([name, item]) => {
    if (["citation_ref", "numeric_citation_ref"].includes(name)) return [name, "<citation-ref>"];
    if (name === "citation_refs") return [name, Array.isArray(item) ? item.map(() => "<citation-ref>") : item];
    if (["observation_id", "evidence_id", "canonical_run_id"].includes(name)) return [name, `<${name.replaceAll("_", "-")}>`];
    if (name === "observation_ids") return [name, Array.isArray(item) ? item.map(() => "<observation-id>") : item];
    if (["as_of", "numeric_as_of"].includes(name)) return [name, "<anchor-as-of>"];
    return [name, stableEvidence(item)];
  }));
}
