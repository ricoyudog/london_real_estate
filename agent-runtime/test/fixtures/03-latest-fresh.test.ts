import assert from "node:assert/strict";
import test from "node:test";

import { artifact, assertCalls, citationArgs, finishText, numericDraft, runFixture, tool, toolAfterResult } from "./shared.ts";

test("latest fresh Bank Rate completes from canonical query and citation without refresh", async () => {
  const fixture = await runFixture({
    fixture: "3",
    prompt: "Give the latest Bank Rate.",
    responses: (launcher) => [
      tool("query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 }),
      toolAfterResult("get_citation_metadata", () => citationArgs(launcher)),
      toolAfterResult("finalize_market_brief", () => numericDraft(launcher, "complete")),
      finishText(),
    ],
  });

  assert.equal(artifact(fixture.outcome).status, "complete");
  assert.equal(fixture.launcher.calls.some((call) => call.toolName === "request_data_refresh"), false);
  assertCalls(fixture, ["query_market_data", "get_citation_metadata", "finalize_market_brief"]);
});
