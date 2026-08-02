import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";
import test from "node:test";

import { BootError, bootRuntime } from "../src/boot.ts";
import type { SessionContext } from "../src/runtime.ts";

const context: SessionContext = {
  principal: "smoke-operator",
  capability_scope_id: "smoke-scope",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current"],
  allowed_refresh_profiles: ["bank-rate-latest"],
};
const expectedTools = [
  "describe_market_data",
  "query_market_data",
  "get_citation_metadata",
  "request_data_refresh",
  "get_refresh_status",
  "finalize_market_brief",
] as const;

test("real-model smoke gate is opt-in and never substitutes for deterministic gates", async (t) => {
  if (process.env.RUN_REAL_MODEL_SMOKE !== "1") {
    t.skip("RUN_REAL_MODEL_SMOKE not set");
    return;
  }

  const priorModel = process.env.PI_MODEL;
  const priorDataDir = process.env.CRE_DATA_DIR;
  let sentinelReached = false;
  const sentinelModel = { provider: "smoke", id: "sentinel" };
  const sentinelModels = {
    getModel: (provider: string, model: string) => {
      sentinelReached = provider === sentinelModel.provider && model === sentinelModel.id;
      return sentinelModel;
    },
  };

  try {
    delete process.env.PI_MODEL;
    delete process.env.CRE_DATA_DIR;
    await assert.rejects(bootRuntime(context, { modelsOverride: sentinelModels }), new BootError("PI_MODEL required"));

    process.env.PI_MODEL = "smoke/sentinel";
    await assert.rejects(bootRuntime(context, { modelsOverride: sentinelModels }), new BootError("CRE_DATA_DIR required"));

    process.env.CRE_DATA_DIR = mkdtempSync(join(tmpdir(), "real-model-smoke-"));
    await bootRuntime(context, {
      modelsOverride: sentinelModels,
      createSession: async () => ({ session: { activeToolNames: expectedTools } }),
    });

    assert.ok(sentinelReached);
    t.diagnostic("preflight sentinel reached; no live inference");
  } finally {
    restoreEnv("PI_MODEL", priorModel);
    restoreEnv("CRE_DATA_DIR", priorDataDir);
  }
});

function restoreEnv(name: "PI_MODEL" | "CRE_DATA_DIR", value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}
