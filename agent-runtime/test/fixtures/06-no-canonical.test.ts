import assert from "node:assert/strict";
import test from "node:test";

import { assertCalls, finishText, runFixture, tool, toolAfterResult } from "./shared.ts";

test("missing canonical Bank Rate describes availability and finalizes unavailable without query or refresh", async () => {
  const fixture = await runFixture({
    fixture: "6",
    prompt: "Give the latest Bank Rate.",
    empty: true,
    responses: () => [
      tool("describe_market_data", {}),
      toolAfterResult("finalize_market_brief", () => ({ title: "Bank Rate unavailable", status: "unavailable", facts: [], inferences: [], limitations: ["Canonical Bank Rate coverage is unavailable."] })),
      finishText(),
    ],
  });

  assert.ok(typeof fixture.outcome.artifact === "object" && fixture.outcome.artifact !== null);
  assert.equal(Reflect.get(fixture.outcome.artifact, "status"), "unavailable");
  assert.equal(fixture.launcher.calls.some((call) => ["query_market_data", "request_data_refresh"].includes(call.toolName)), false);
  assertCalls(fixture, ["describe_market_data", "finalize_market_brief"]);
});
