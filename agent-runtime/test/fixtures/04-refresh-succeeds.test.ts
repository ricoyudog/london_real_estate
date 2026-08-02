import assert from "node:assert/strict";
import test from "node:test";

import { artifact, assertCalls, citationArgs, finishText, numericDraft, ok, runFixture, tool, toolAfterResult } from "./shared.ts";

test("latest stale refresh enforces cadence and re-queries after terminal success", async () => {
  const fixture = await runFixture({
    fixture: "4",
    prompt: "Give the latest Bank Rate after refreshing stale data.",
    stale: true,
    refreshScript: {
      request_data_refresh: [ok({ disposition: "accepted", initial_state: "pending", job_ref: "job-fixed", poll_after_seconds: 15 })],
      get_refresh_status: [
        ok({ job_ref: "job-fixed", job_state: "pending", canonical_changed: null }),
        ok({ job_ref: "job-fixed", job_state: "succeeded", canonical_changed: true }),
      ],
    },
    responses: (launcher, clock) => [
      tool("query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 }),
      toolAfterResult("request_data_refresh", () => ({ capability_id: "uk.bank-rate-current", datasource_id: "boe.bank_rate.iudbedr", request_profile: "bank-rate-latest", bounded_scope: {}, intent: "refresh latest canonical rate" })),
      toolAfterResult("get_refresh_status", () => ({ job_ref: "job-fixed" })),
      toolAfterResult("get_refresh_status", () => ({ job_ref: "job-fixed" }), () => clock.advance(15_000)),
      toolAfterResult("get_refresh_status", () => ({ job_ref: "job-fixed" }), () => clock.advance(15_000)),
      toolAfterResult("query_market_data", () => ({ capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 })),
      toolAfterResult("get_citation_metadata", () => citationArgs(launcher)),
      toolAfterResult("finalize_market_brief", () => numericDraft(launcher, "complete")),
      finishText(),
    ],
  });

  assert.equal(fixture.launcher.calls.filter((call) => call.toolName === "get_refresh_status").length, 2);
  assert.equal(fixture.launcher.calls.filter((call) => call.toolName === "query_market_data").length, 2);
  assert.equal(artifact(fixture.outcome).facts[0]?.numeric_value, "5.25");
  assertCalls(fixture, ["query_market_data", "request_data_refresh", "get_refresh_status", "get_refresh_status", "get_refresh_status", "query_market_data", "get_citation_metadata", "finalize_market_brief"]);
});
