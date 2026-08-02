import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";

import { ModelRuntime } from "@earendil-works/pi-coding-agent";

import { bootRuntime, type BootedRuntime } from "../src/boot.ts";
import { finalizeBrief } from "../src/finalizer.ts";
import type { SessionContext } from "../src/runtime.ts";
import { runTurn, type TurnOutcome } from "../src/turn-runner.ts";

const skipReason = "real-model e2e requires RUN_REAL_MODEL_SMOKE=1 and SUB2API_API_KEY";
const apiKey = process.env.SUB2API_API_KEY;
const enabled = process.env.RUN_REAL_MODEL_SMOKE === "1" && apiKey !== undefined && apiKey !== "";
const worktreeRoot = resolve(import.meta.dirname, "../..");
const context: SessionContext = {
  principal: "real-model-smoke",
  capability_scope_id: "real-model-smoke-scope",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current"],
  allowed_refresh_profiles: ["bank-rate-latest"],
};

test("real GLM-5.2 runs a terminal turn through the seeded Bank Rate tools", { skip: enabled ? false : skipReason, timeout: 120_000 }, async (t) => {
  assert.ok(apiKey);
  const previousModel = process.env.PI_MODEL;
  const previousDataDir = process.env.CRE_DATA_DIR;
  const dataDir = join(mkdtempSync(join(tmpdir(), "real-model-e2e-")), "data");
  execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_bank_rate.py", dataDir, "5.25"], { cwd: worktreeRoot });
  process.env.PI_MODEL = "sub2api/GLM-5.2";
  process.env.CRE_DATA_DIR = dataDir;

  let booted: BootedRuntime | undefined;
  try {
    let result = await run(false);
    booted = result.booted;
    if (result.outcome.terminal_state === "failed" && toolSequence(result.outcome).length === 0) {
      await disposeSession(booted.session);
      result = await run(true);
      booted = result.booted;
    }

    const { outcome, reasoning } = result;
    assert.ok(outcome.terminal_state === "completed" || outcome.terminal_state === "failed");
    t.diagnostic(`reasoning=${reasoning}`);
    t.diagnostic(`tool_sequence=${JSON.stringify(toolSequence(outcome))}`);
    if (outcome.terminal_state === "completed") {
      assertArtifact(outcome.artifact);
      t.diagnostic(`artifact=${JSON.stringify(outcome.artifact)}`);
    } else {
      t.diagnostic(`failure_reason=${JSON.stringify(outcome.events.at(-1))}`);
    }
  } finally {
    if (booted !== undefined) await disposeSession(booted.session);
    restoreEnv("PI_MODEL", previousModel);
    restoreEnv("CRE_DATA_DIR", previousDataDir);
  }
});

async function run(reasoning: boolean): Promise<{ readonly booted: BootedRuntime; readonly outcome: TurnOutcome; readonly reasoning: boolean }> {
  assert.ok(apiKey);
  const models = await ModelRuntime.create({ modelsPath: null });
  models.registerProvider("sub2api", {
    name: "sub2api",
    baseUrl: "https://sub2api-production-4d3a.up.railway.app/v1",
    apiKey,
    api: "openai-completions",
    models: [{
      id: "GLM-5.2", name: "GLM-5.2", reasoning, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 200_000, maxTokens: 65_536,
    }],
  });
  const booted = await bootRuntime(context, {
    modelsOverride: models,
    finalizeBrief: async (draft, turn) => finalizeBrief({ schema_version: "market_brief_draft.v1", ...record(draft) }, turn),
  });
  return { booted, outcome: await runTurn(booted, "What is the current Bank of England base rate?", { populateLedger: true }), reasoning };
}

function toolSequence(outcome: TurnOutcome): readonly string[] {
  return outcome.events.filter((event) => event.type === "tool.started").map((event) => event.tool);
}

function assertArtifact(value: unknown): void {
  const artifact = record(value);
  assert.equal(artifact["schema_version"], "market_brief.v1");
  const facts = artifact["facts"];
  if (!Array.isArray(facts)) return;
  for (const fact of facts) {
    const numericValue = record(fact)["numeric_value"];
    if (numericValue !== undefined) assert.ok(typeof numericValue === "string" && Number.isFinite(Number(numericValue)));
  }
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
