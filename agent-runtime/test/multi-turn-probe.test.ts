// Multi-turn probe: drives the real GLM-5.2 runtime through 3 user turns that
// exercise the in-scope Bank Rate coverage and the deliberately-blocked London
// CRE coverage. Opt-in only — set RUN_REAL_MODEL_SMOKE=1 plus PI_* env.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";

import { bootRuntime, type BootedRuntime } from "../src/boot.ts";
import { finalizeBrief } from "../src/finalizer.ts";
import type { SessionContext } from "../src/runtime.ts";
import { runTurn, type TurnOutcome } from "../src/turn-runner.ts";

const enabled = process.env.RUN_REAL_MODEL_SMOKE === "1";
const worktreeRoot = resolve(import.meta.dirname, "../..");
const context: SessionContext = {
  principal: "multi-turn-probe",
  capability_scope_id: "multi-turn-scope",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current", "london-planning-activity"],
  allowed_refresh_profiles: ["bank-rate-latest", "planning-activity-monthly"],
};
const QUESTIONS = [
  "What is the current Bank of England base rate? Cite the source.",
  "What is the average prime rent for Grade A office space in Mayfair right now?",
  "What is the current office vacancy rate in the City of London?",
  "How many planning applications were decided in London boroughs in July 2026? Cite the source.",
] as const;

test("multi-turn probe: in-scope Bank Rate then two blocked London CRE asks plus in-scope PLD", { skip: enabled ? false : "RUN_REAL_MODEL_SMOKE=1 required", timeout: 300_000 }, async (t) => {
  const previousDataDir = process.env.CRE_DATA_DIR;
  const dataDir = join(mkdtempSync(join(tmpdir(), "mt-probe-")), "data");
  const { execFileSync } = await import("node:child_process");
  execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_bank_rate.py", dataDir, "5.25"], { cwd: worktreeRoot });
  execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_pld_activity.py", dataDir], { cwd: worktreeRoot });
  process.env.CRE_DATA_DIR = dataDir;

  let booted: BootedRuntime | undefined;
  const outcomes: TurnOutcome[] = [];
  try {
    booted = await bootRuntime(context, {
      finalizeBrief: async (draft, turn) => finalizeBrief({ schema_version: "market_brief_draft.v1", ...record(draft) }, turn),
    });
    for (const q of QUESTIONS) {
      t.diagnostic(`>>> ${q}`);
      const outcome = await runTurn(booted, q, { populateLedger: true });
      outcomes.push(outcome);
      const seq = outcome.events.filter((e) => e.type === "tool.started").map((e) => e.tool);
      const summary = artifactSummary(outcome.artifact);
      t.diagnostic(`terminal=${outcome.terminal_state} tools=${JSON.stringify(seq)} artifact=${JSON.stringify(summary)}`);
      if (outcome.terminal_state !== "completed") {
        t.diagnostic(`reason_code=${outcome.reason_code ?? "<none>"} clarification=${outcome.clarification_requested ?? false}`);
      }
    }
    // Q1 in-scope: must complete with a numeric fact and at least one source.
    assert.equal(outcomes[0].terminal_state, "completed", "Q1 Bank Rate must reach a completed turn");
    assert.ok(/5\.25/.test(JSON.stringify(outcomes[0].artifact)), "Q1 artifact must carry the seeded 5.25 value");
    // Q2/Q3 out-of-scope: turns must terminate and must NOT fabricate CRE numbers.
    for (const idx of [1, 2] as const) {
      const o = outcomes[idx];
      assert.notEqual(o.terminal_state, "awaiting_approval", `Q${idx + 1} should not stall on approval`);
      const artifact = record(o.artifact);
      const facts = Array.isArray(artifact["facts"]) ? artifact["facts"] : [];
      const leakedNumerics = facts.flatMap((f) => {
        const v = record(f)["numeric_value"];
        return typeof v === "string" && Number.isFinite(Number(v)) ? [v] : [];
      });
      // Office rent or vacancy numbers should never appear. Allow empty or status=coverage_unavailable.
      assert.ok(
        leakedNumerics.length === 0 || /unavailable|blocked|out_of_scope/i.test(String(artifact["status"] ?? "")),
        `Q${idx + 1} must not surface fabricated CRE numerics; got ${JSON.stringify(leakedNumerics)} status=${artifact["status"]}`,
      );
    }
    // Q4 in-scope PLD: must complete with at least one numeric fact (Camden=3 or City of London=2).
    const q4 = outcomes[3];
    assert.equal(q4.terminal_state, "completed", "Q4 PLD must reach a completed turn");
    const q4Artifact = record(q4.artifact);
    const q4Facts = Array.isArray(q4Artifact["facts"]) ? q4Artifact["facts"] : [];
    const q4Numerics = q4Facts.flatMap((f) => {
      const v = record(f)["numeric_value"];
      return typeof v === "string" && Number.isFinite(Number(v)) ? [v] : [];
    });
    assert.ok(q4Numerics.length > 0, `Q4 must surface at least one numeric PLD fact; got ${JSON.stringify(q4Numerics)}`);
    assert.ok(q4Numerics.some((v) => v === "3" || v === "2" || v === "5"), `Q4 must surface the seeded Camden/City-of-London counts; got ${JSON.stringify(q4Numerics)}`);
    assert.ok(Array.isArray(q4Artifact["sources"]) && q4Artifact["sources"].length > 0, "Q4 must cite at least one source");
  } finally {
    if (booted !== undefined) {
      const s = booted.session as { dispose?: () => Promise<void> };
      if (typeof s.dispose === "function") await s.dispose();
    }
    if (previousDataDir === undefined) delete process.env.CRE_DATA_DIR;
    else process.env.CRE_DATA_DIR = previousDataDir;
  }
});

function artifactSummary(value: unknown): Readonly<Record<string, unknown>> {
  if (value === undefined) return { artifact: "<undefined>" };
  const artifact = record(value);
  const facts = Array.isArray(artifact["facts"]) ? artifact["facts"] : [];
  return {
    schema_version: artifact["schema_version"],
    status: artifact["status"],
    fact_count: facts.length,
    numeric_values: facts.flatMap((f) => {
      const v = record(f)["numeric_value"];
      return typeof v === "string" ? [v] : [];
    }),
    source_count: Array.isArray(artifact["sources"]) ? artifact["sources"].length : 0,
    as_of: artifact["as_of"],
  };
}
function record(value: unknown): Readonly<Record<string, unknown>> {
  assert.ok(typeof value === "object" && value !== null && !Array.isArray(value));
  return value as Readonly<Record<string, unknown>>;
}
