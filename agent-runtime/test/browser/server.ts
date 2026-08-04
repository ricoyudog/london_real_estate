import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";

import type { BootOptions } from "../../src/boot.ts";
import { createApp } from "../../src/app.ts";
import { FacadeLauncher } from "../../src/facade-launcher.ts";

// ponytail: one Python process for both seeders — pays uv startup + nan_fung
// import once; sequential calls are safe on the single-writer OperationalStore.
const SEED_BOTH = [
  "import sys",
  "from pathlib import Path",
  "sys.path.insert(0, str(Path('agent-runtime/test/helpers').resolve()))",
  "from seed_bank_rate import seed_bank_rate",
  "from seed_pld_activity import seed_pld_activity",
  'seed_bank_rate(Path(sys.argv[1]), "5.25")',
  "seed_pld_activity(Path(sys.argv[1]))",
].join("; ");

const runtimeRoot = resolve(import.meta.dirname, "../..");
const worktreeRoot = resolve(runtimeRoot, "..");
const temporaryRoot = mkdtempSync(join(tmpdir(), "market-desk-browser-"));
const dataDir = join(temporaryRoot, "data");
const facadeBridge = resolve(runtimeRoot, "test/helpers/facade-fd3-bridge.py");

execFileSync("uv", ["run", "python", "-c", SEED_BOTH, dataDir], { cwd: worktreeRoot });
process.env.PI_MODEL = "faux/model";

const activeToolNames = [
  "describe_market_data",
  "query_market_data",
  "get_citation_metadata",
  "request_data_refresh",
  "get_refresh_status",
  "finalize_market_brief",
] as const;

const app = await createApp({
  ctx: {
    principal: "browser-test",
    capability_scope_id: "browser-test",
    allowed_access_classes: ["open"],
    allowed_capability_ids: ["uk.bank-rate-current", "london-planning-activity"],
    allowed_refresh_profiles: [],
  },
  creDataDir: dataDir,
  launcher: new FacadeLauncher({ creDataDir: dataDir, binaryPath: facadeBridge }),
  modelsOverride: { getModel: () => ({ provider: "faux", id: "model" }) },
  createSession: scriptedSession,
  deployment: { mode: "demo", fixture_label: "Deterministic Bank Rate fixture" },
  trace: () => undefined,
});

await new Promise<void>((resolveListen, rejectListen) => {
  app.server.once("error", rejectListen);
  app.server.listen(8799, "127.0.0.1", () => {
    app.server.off("error", rejectListen);
    resolveListen();
  });
});
console.log("browser fixture ready at http://127.0.0.1:8799");

let closing = false;
const close = (): void => {
  if (closing) return;
  closing = true;
  void app.close().finally(() => {
    rmSync(temporaryRoot, { recursive: true, force: true });
    process.exit(0);
  });
};
process.once("SIGINT", close);
process.once("SIGTERM", close);

async function scriptedSession(options: Parameters<NonNullable<BootOptions["createSession"]>>[0]): Promise<{ readonly session: unknown }> {
  const tools = options.customTools ?? [];
  const listeners = new Set<(event: Readonly<Record<string, unknown>>) => void>();
  let callIndex = 0;
  const emit = (event: Readonly<Record<string, unknown>>): void => {
    for (const listener of listeners) listener(event);
  };
  const call = async (name: string, args: Readonly<Record<string, unknown>>): Promise<Readonly<Record<string, unknown>>> => {
    const tool = tools.find((candidate) => candidate.name === name);
    if (tool === undefined) throw new Error(`missing browser fixture tool: ${name}`);
    callIndex += 1;
    emit({ type: "tool_execution_start", toolName: name });
    const result = await Reflect.apply(tool.execute, tool, [`browser_${callIndex}`, args, undefined, undefined, undefined]);
    emit({ type: "tool_execution_end", toolName: name, result });
    return record(record(result)["details"]);
  };
  const session = {
    activeToolNames,
    subscribe(listener: (event: Readonly<Record<string, unknown>>) => void): () => void {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    async prompt(message: string): Promise<void> {
      if (message.includes("[FAIL]")) {
        emit({ type: "message_update", assistantMessageEvent: { type: "text_delta", delta: "4%" } });
        return;
      }
      if (message.includes("[SLOW]")) {
        await delay(1_500);
        return;
      }
      if (message.includes("[DELAY]")) await delay(1_000);
      if (/vacancy|rent|office overview/i.test(message)) {
        await call("describe_market_data", {});
        await call("finalize_market_brief", {
          title: "Coverage brief",
          status: "unavailable",
          facts: [],
          inferences: [],
          limitations: ["The requested office-market coverage is not available in the canonical launch scope."],
        });
        return;
      }
      if (/project supply/i.test(message)) {
        await call("describe_market_data", {});
        await call("finalize_market_brief", {
          title: "Project supply brief",
          status: "unavailable",
          facts: [],
          inferences: [],
          limitations: ["The requested project-supply coverage is not available in the canonical launch scope."],
        });
        return;
      }
      if (/City of London.*July 2026/i.test(message)) {
        const query = await call("query_market_data", {
          capability_id: "london-planning-activity",
          query_kind: "metrics",
          filters: { geography_code: "203", source_date_from: "2026-07-01", source_date_to: "2026-07-31" },
          as_of: "2026-08-01T12:00:00Z",
          limit: 1,
        });
        const records = array(record(query["data"])["records"]);
        const first = record(records[0]);
        const citationRefs = array(first["citation_refs"]).filter((value): value is string => typeof value === "string");
        await call("get_citation_metadata", { citation_refs: citationRefs });
        await call("finalize_market_brief", {
          title: "City planning activity",
          status: "complete",
          facts: [{ claim_id: "planning-activity", kind: "numeric", confidence: "medium", numeric_citation_ref: citationRefs[0] }],
          inferences: [],
          limitations: [],
        });
        return;
      }
      const query = await call("query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 });
      const records = array(record(query["data"])["records"]);
      const first = record(records[0]);
      const citationRefs = array(first["citation_refs"]).filter((value): value is string => typeof value === "string");
      await call("get_citation_metadata", { citation_refs: citationRefs });
      await call("finalize_market_brief", {
        title: "Bank Rate brief",
        status: "complete",
        facts: [{ claim_id: "bank-rate", kind: "numeric", confidence: "medium", numeric_citation_ref: citationRefs[0] }],
        inferences: [{ claim_id: "outlook", text: "The policy outlook remains uncertain.", confidence: "low", supporting_fact_ids: ["bank-rate"], caveat: "Conditions may change." }],
        limitations: [],
      });
    },
    async abort(): Promise<void> { return; },
    async dispose(): Promise<void> { listeners.clear(); },
  };
  return { session };
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new TypeError("browser fixture expected an object");
  return value as Readonly<Record<string, unknown>>;
}

function array(value: unknown): readonly unknown[] { return Array.isArray(value) ? value : []; }
function delay(milliseconds: number): Promise<void> { return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds)); }
