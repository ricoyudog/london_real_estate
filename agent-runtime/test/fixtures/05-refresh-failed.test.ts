import assert from "node:assert/strict";
import test from "node:test";

import { artifact, assertCalls, citationArgs, finishText, numericDraft, ok, runFixture, tool, toolAfterResult } from "./shared.ts";

test("failed refresh retains last-good canonical data with an explicit freshness warning", async () => {
  const fixture = await runFixture({
    fixture: "5",
    prompt: "Give the latest Bank Rate after checking stale data.",
    stale: true,
    refreshScript: {
      request_data_refresh: [ok({ disposition: "accepted", initial_state: "pending", job_ref: "job-failed", poll_after_seconds: 15 })],
      get_refresh_status: [ok({ job_ref: "job-failed", job_state: "dead_letter", canonical_changed: false })],
    },
    responses: (launcher, clock) => [
      tool("query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 }),
      toolAfterResult("request_data_refresh", () => ({ capability_id: "uk.bank-rate-current", datasource_id: "boe.bank_rate.iudbedr", request_profile: "bank-rate-latest", bounded_scope: {}, intent: "refresh latest canonical rate" })),
      toolAfterResult("get_refresh_status", () => ({ job_ref: "job-failed" }), () => clock.advance(15_000)),
      toolAfterResult("query_market_data", () => ({ capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 })),
      toolAfterResult("get_citation_metadata", () => citationArgs(launcher)),
      toolAfterResult("finalize_market_brief", () => numericDraft(launcher, "partial", ["Freshness is degraded."])),
      finishText(),
    ],
  });

  const brief = artifact(fixture.outcome);
  assert.equal(brief.status, "partial");
  assert.equal(brief.facts[0]?.numeric_value, "5.25");
  assert.ok(brief.freshness_warnings.length > 0);
  assertCalls(fixture, ["query_market_data", "request_data_refresh", "get_refresh_status", "query_market_data", "get_citation_metadata", "finalize_market_brief"]);
});
