import assert from "node:assert/strict";
import test from "node:test";

import { assertCalls, runFixture } from "./shared.ts";

test("ambiguous Bank Rate date requests clarification without entering Pi or the facade", async () => {
  const fixture = await runFixture({
    fixture: "1",
    prompt: "What is the Bank of England base rate?",
    responses: () => [],
  });

  assert.equal(fixture.outcome.clarification_requested, true);
  assert.equal(fixture.launcher.calls.length, 0);
  assert.equal(fixture.fauxCalls, 0);
  assertCalls(fixture, []);
});
