import assert from "node:assert/strict";
import test from "node:test";

import { artifact, assertCalls, citationArgs, finishText, numericDraft, runFixture, tool, toolAfterResult } from "./shared.ts";

test("explicit historical Bank Rate queries and cites the seeded canonical value without refresh", async () => {
  const fixture = await runFixture({
    fixture: "2",
    prompt: "Give the Bank Rate as of 2026-08-02T00:00:00Z.",
    responses: (launcher) => [
      tool("query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics", as_of: "2026-08-02T00:00:00Z", limit: 1 }),
      toolAfterResult("get_citation_metadata", () => citationArgs(launcher)),
      toolAfterResult("finalize_market_brief", () => numericDraft(launcher, "partial", ["Macro-only coverage."])),
      finishText(),
    ],
  });

  const brief = artifact(fixture.outcome);
  assert.equal(brief.status, "partial");
  assert.equal(brief.facts[0]?.numeric_value, "5.25");
  assert.deepEqual(Object.keys(brief.lineage), ["bank-rate"]);
  assert.equal(fixture.launcher.calls.some((call) => call.toolName === "request_data_refresh"), false);
  assertCalls(fixture, ["query_market_data", "get_citation_metadata", "finalize_market_brief"]);
});

test("sentinel proves boot uses real createAgentSession, custom tool execute, and faux continuation context", async () => {
  let observedToolResult = false;
  const fixture = await runFixture({
    fixture: "sentinel",
    prompt: "Give the latest Bank Rate.",
    responses: (launcher) => [
      tool("query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 }),
      toolAfterResult("get_citation_metadata", () => citationArgs(launcher), () => { observedToolResult = true; }),
      toolAfterResult("finalize_market_brief", () => numericDraft(launcher, "complete")),
      finishText(),
    ],
  });

  assert.equal(fixture.createSessionCalls, 1);
  assert.ok(fixture.launcher.calls.some((call) => call.toolName === "query_market_data"));
  assert.equal(observedToolResult, true);
  assert.equal(fixture.fauxCalls, 4);
});
