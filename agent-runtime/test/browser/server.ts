import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";

import type { BootOptions } from "../../src/boot.ts";
import { createApp } from "../../src/app.ts";

const runtimeRoot = resolve(import.meta.dirname, "../..");
const worktreeRoot = resolve(runtimeRoot, "..");
const temporaryRoot = mkdtempSync(join(tmpdir(), "market-desk-browser-"));
const dataDir = join(temporaryRoot, "data");

execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_bank_rate.py", dataDir, "5.25"], { cwd: worktreeRoot });
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
    allowed_capability_ids: ["uk.bank-rate-current"],
    allowed_refresh_profiles: [],
  },
  creDataDir: dataDir,
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
