import assert from "node:assert/strict";
import test from "node:test";

import { assertCalls, finishText, runFixture, tool, toolAfterResult } from "./shared.ts";

test("TC-01 blocked coverage describes only and returns no numeric fact or citation", async () => {
  const fixture = await runFixture({
    fixture: "7",
    prompt: "What is the latest London City prime office rent?",
    responses: () => [
      tool("describe_market_data", {}),
      toolAfterResult("finalize_market_brief", () => ({ title: "Prime rent unavailable", status: "unavailable", facts: [], inferences: [], limitations: ["Current launch coverage does not include London prime rent."] })),
      finishText(),
    ],
  });

  assert.ok(typeof fixture.outcome.artifact === "object" && fixture.outcome.artifact !== null);
  assert.deepEqual(Reflect.get(fixture.outcome.artifact, "facts"), []);
  assert.deepEqual(Reflect.get(fixture.outcome.artifact, "sources"), []);
  assert.equal(fixture.launcher.calls.some((call) => ["query_market_data", "request_data_refresh"].includes(call.toolName)), false);
  assertCalls(fixture, ["describe_market_data", "finalize_market_brief"]);
});
