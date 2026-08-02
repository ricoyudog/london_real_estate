import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";

import { bootRuntime, type BootedRuntime } from "../src/boot.ts";
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
  allowed_capability_ids: ["uk.bank-rate-current"],
  allowed_refresh_profiles: ["bank-rate-latest"],
};

test("real GLM-5.2 runs a terminal turn through the seeded Bank Rate tools", { skip: enabled ? false : skipReason, timeout: 120_000 }, async (t) => {
  const previousDataDir = process.env.CRE_DATA_DIR;
  const dataDir = join(mkdtempSync(join(tmpdir(), "real-model-e2e-")), "data");
  execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_bank_rate.py", dataDir, "5.25"], { cwd: worktreeRoot });
  process.env.CRE_DATA_DIR = dataDir;

  let booted: BootedRuntime | undefined;
  try {
    const result = await run();
    booted = result.booted;
    const { outcome } = result;
    t.diagnostic(`tool_sequence=${JSON.stringify(toolSequence(outcome))}`);
    if (outcome.terminal_state !== "completed") t.diagnostic(`failure_reason=${JSON.stringify(outcome.events.at(-1))}`);
    assert.equal(outcome.terminal_state, "completed");
    assertArtifact(outcome.artifact);
    assertMinimumToolSequence(toolSequence(outcome));
    t.diagnostic(`artifact_summary=${JSON.stringify(artifactSummary(outcome.artifact))}`);
  } finally {
    if (booted !== undefined) await disposeSession(booted.session);
    restoreEnv("CRE_DATA_DIR", previousDataDir);
  }
});

async function run(): Promise<{ readonly booted: BootedRuntime; readonly outcome: TurnOutcome }> {
  const booted = await bootRuntime(context, {
    finalizeBrief: async (draft, turn) => finalizeBrief({ schema_version: "market_brief_draft.v1", ...record(draft) }, turn),
  });
  return { booted, outcome: await runTurn(booted, "What is the current Bank of England base rate?", { populateLedger: true }) };
}

function assertMinimumToolSequence(sequence: readonly string[]): void {
  for (const tool of ["query_market_data", "get_citation_metadata", "finalize_market_brief"] as const) assert.ok(sequence.includes(tool), `missing required tool: ${tool}`);
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
  for (const fact of facts) {
    const numericValue = record(fact)["numeric_value"];
    if (numericValue !== undefined) assert.ok(typeof numericValue === "string" && Number.isFinite(Number(numericValue)));
  }
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
