import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";

import { bootRuntime, type BootedRuntime } from "../src/boot.ts";
import { FacadeLauncher, type ToolResult } from "../src/facade-launcher.ts";
import { finalizeBrief } from "../src/finalizer.ts";
import type { SessionContext } from "../src/runtime.ts";
import { runTurn, type TurnOutcome } from "../src/turn-runner.ts";

const skipReason = "real-model e2e requires RUN_REAL_MODEL_SMOKE=1, PI_MODEL, PI_BASE_URL, and PI_API_KEY";
const enabled = process.env.RUN_REAL_MODEL_SMOKE === "1"
  && process.env.PI_MODEL !== undefined && process.env.PI_MODEL !== ""
  && process.env.PI_BASE_URL !== undefined && process.env.PI_BASE_URL !== ""
  && process.env.PI_API_KEY !== undefined && process.env.PI_API_KEY !== "";
const worktreeRoot = resolve(import.meta.dirname, "../..");
const context: SessionContext = {
  principal: "real-model-smoke",
  capability_scope_id: "real-model-smoke-scope",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current", "london-planning-activity"],
  allowed_refresh_profiles: ["bank-rate-latest", "planning-activity-monthly"],
};

test("real GLM-5.2 runs the City of London July planning path through the actual FacadeLauncher", { skip: enabled ? false : skipReason, timeout: 120_000 }, async (t) => {
  const previousDataDir = process.env.CRE_DATA_DIR;
  const dataDir = join(mkdtempSync(join(tmpdir(), "real-model-e2e-")), "data");
  let booted: BootedRuntime | undefined;
  try {
    execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_pld_activity.py", dataDir], { cwd: worktreeRoot });
    process.env.CRE_DATA_DIR = dataDir;
    const result = await run();
    booted = result.booted;
    const { launcher, outcome } = result;
    t.diagnostic(`tool_sequence=${JSON.stringify(toolSequence(outcome))}`);
    if (outcome.terminal_state !== "completed") t.diagnostic(`failure_reason=${JSON.stringify(outcome.events.at(-1))}`);
    assert.equal(outcome.terminal_state, "completed");
    assertArtifact(outcome.artifact);
    assert.deepEqual(toolSequence(outcome).filter((tool) => ["describe_market_data", "query_market_data", "get_citation_metadata", "finalize_market_brief"].includes(tool)), ["describe_market_data", "query_market_data", "get_citation_metadata", "finalize_market_brief"]);
    assertPlanningCalls(launcher.calls);
    t.diagnostic(`artifact_summary=${JSON.stringify(artifactSummary(outcome.artifact))}`);
  } finally {
    if (booted !== undefined) await disposeSession(booted.session);
    restoreEnv("CRE_DATA_DIR", previousDataDir);
    rmSync(dataDir, { recursive: true, force: true });
  }
});

type Call = { readonly toolName: string; readonly request: unknown; readonly result: ToolResult };

class RecordingFacadeLauncher extends FacadeLauncher {
  readonly calls: Call[] = [];

  override async invoke(toolName: string, request: unknown): Promise<ToolResult> {
    const result = await super.invoke(toolName, request, { timeoutSeconds: 60 });
    this.calls.push({ toolName, request, result });
    return result;
  }
}

async function run(): Promise<{ readonly booted: BootedRuntime; readonly launcher: RecordingFacadeLauncher; readonly outcome: TurnOutcome }> {
  const launcher = new RecordingFacadeLauncher({ creDataDir: requiredEnv("CRE_DATA_DIR") });
  const booted = await bootRuntime(context, {
    launcher,
    finalizeBrief: async (draft, turn) => finalizeBrief({ schema_version: "market_brief_draft.v1", ...record(draft) }, turn),
  });
  return { booted, launcher, outcome: await runTurn(booted, "How many planning applications were decided in City of London in July 2026? Cite the source.", { populateLedger: true }) };
}

function toolSequence(outcome: TurnOutcome): readonly string[] {
  return outcome.events.filter((event) => event.type === "tool.started").map((event) => event.tool);
}

function assertArtifact(value: unknown): void {
  const artifact = record(value);
  assert.equal(artifact["schema_version"], "market_brief.v1");
  const facts = artifact["facts"];
  assert.equal(artifact["status"], "complete");
  assert.ok(Array.isArray(facts) && facts.length > 0);
  assert.ok(Array.isArray(artifact["sources"]) && artifact["sources"].length > 0);
  const numericFacts = facts.map(record).filter((fact) => fact["kind"] === "numeric");
  assert.equal(numericFacts.some((fact) => fact["numeric_definition"] === "Official Bank Rate"), false);
  assert.equal(numericFacts[0]?.["numeric_value"], "2");
  const sources = array(artifact["sources"]).map(record);
  assert.equal(new URL(String(sources[0]?.["public_url"])).host, "files.planning.data.gov.uk");
  assert.ok(Object.keys(record(artifact["lineage"])).length > 0);
  assert.ok(array(artifact["limitations"]).some((limitation) => typeof limitation === "string" && limitation.includes("all use classes")));
}

function assertPlanningCalls(calls: readonly Call[]): void {
  const query = calls.find((call) => call.toolName === "query_market_data");
  assert.ok(query !== undefined);
  assert.deepEqual(record(query.request).arguments, {
    capability_id: "london-planning-activity",
    query_kind: "metrics",
    filters: { geography_code: "203", source_date_from: "2026-07-01", source_date_to: "2026-07-31" },
    as_of: "2026-08-01T12:00:00Z",
    limit: 1,
  });
  const queryData = record(query.result.data);
  assert.equal(queryData["capability_id"], "london-planning-activity");
  assert.deepEqual(queryData["datasource_ids"], ["pld.applications_search"]);
  assert.equal(queryData["result_count"], 1);
  const first = record(array(queryData["records"])[0]);
  assert.equal(first["datasource_id"], "pld.applications_search");
  assert.equal(record(first["numeric"])["value"], "2");
}

function artifactSummary(value: unknown): Readonly<Record<string, unknown>> {
  const artifact = record(value);
  const facts = Array.isArray(artifact["facts"]) ? artifact["facts"] : [];
  return {
    schema_version: artifact["schema_version"],
    status: artifact["status"],
    fact_count: facts.length,
    numeric_values: facts.flatMap((fact) => {
      const numericValue = record(fact)["numeric_value"];
      return typeof numericValue === "string" ? [numericValue] : [];
    }),
    source_count: Array.isArray(artifact["sources"]) ? artifact["sources"].length : 0,
    as_of: artifact["as_of"],
  };
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  assert.ok(isRecord(value));
  return value;
}

function array(value: unknown): readonly unknown[] {
  assert.ok(Array.isArray(value));
  return value;
}

function requiredEnv(name: "CRE_DATA_DIR"): string {
  const value = process.env[name];
  assert.ok(value !== undefined && value !== "");
  return value;
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function disposeSession(value: unknown): Promise<void> {
  if (typeof value === "object" && value !== null && "dispose" in value && typeof value.dispose === "function") await value.dispose();
}

function restoreEnv(name: "PI_MODEL" | "CRE_DATA_DIR", value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}
