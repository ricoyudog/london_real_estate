import assert from "node:assert/strict";
import test from "node:test";

import { assertCalls, finishText, runFixture, tool, toolAfterResult } from "./shared.ts";

test("time-free Bank Rate questions enter Pi instead of a host-side clarification", async () => {
  const fixture = await runFixture({
    fixture: "1",
    prompt: "What is the Bank of England base rate?",
    responses: () => [
      tool("describe_market_data", {}),
      toolAfterResult("finalize_market_brief", () => ({
        title: "Bank Rate coverage",
        status: "unavailable",
        facts: [],
        inferences: [],
        limitations: ["This fixture verifies that Pi receives the unanchored request."],
      })),
      finishText(),
    ],
  });

  assert.equal(fixture.outcome.clarification_requested, undefined);
  assert.ok(fixture.fauxCalls > 0);
  assertCalls(fixture, ["describe_market_data", "finalize_market_brief"]);
});
