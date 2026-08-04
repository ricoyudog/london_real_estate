import assert from "node:assert/strict";
import test from "node:test";

import { artifact, assertCalls, finishText, runFixture, tool, toolAfterResult } from "./shared.ts";

const planningQuestion = "How many planning applications were decided in City of London in July 2026? Cite the source.";
const planningArgs = {
  capability_id: "london-planning-activity",
  query_kind: "metrics",
  filters: { geography_code: "203", source_date_from: "2026-07-01", source_date_to: "2026-07-31" },
  as_of: "2026-08-01T12:00:00Z",
};

test("planning activity fixture completes through ordered describe query citation finalize", async () => {
  const fixture = await runFixture({
    fixture: "8-planning",
    prompt: planningQuestion,
    seed: "planning",
    capabilityIds: ["uk.bank-rate-current", "london-planning-activity"],
    refreshProfiles: ["bank-rate-latest", "planning-activity-monthly"],
    responses: (launcher) => [
      tool("describe_market_data", {}),
      tool("query_market_data", { ...planningArgs, limit: 1 }),
      toolAfterResult("get_citation_metadata", () => {
        const records = launcher.calls.find((call) => call.toolName === "query_market_data")?.result.data?.records;
        assert.ok(Array.isArray(records));
        return { citation_refs: records[0]?.citation_refs };
      }),
      toolAfterResult("finalize_market_brief", () => {
        const records = launcher.calls.find((call) => call.toolName === "query_market_data")?.result.data?.records;
        assert.ok(Array.isArray(records));
        return {
          title: "City planning activity",
          status: "complete",
          facts: [{ claim_id: "planning", kind: "numeric", confidence: "high", numeric_citation_ref: records[0]?.citation_refs[0] }],
          inferences: [],
          limitations: [],
        };
      }),
      finishText(),
    ],
  });

  assert.equal(fixture.createSessionCalls, 1);
  assertCalls(fixture, ["describe_market_data", "query_market_data", "get_citation_metadata", "finalize_market_brief"]);
  const query = fixture.launcher.calls.find((call) => call.toolName === "query_market_data");
  assert.ok(query !== undefined);
  assert.deepEqual(record(query.request).arguments, { ...planningArgs, limit: 1 });
  const brief = artifact(fixture.outcome);
  assert.equal(brief.status, "complete");
  assert.equal(brief.facts[0]?.numeric_value, "2");
  assert.equal(brief.sources[0]?.public_url, "https://files.planning.data.gov.uk/dataset/planning-application.csv");
  assert.deepEqual(brief.lineage["planning"]?.observation_ids.length, 1);
  assert.ok(brief.limitations.some((limitation) => limitation.includes("all use classes")));
});

test("planning activity fixture attributes no canonical result to unavailable planning coverage", async () => {
  const fixture = await runFixture({
    fixture: "8-planning-empty",
    prompt: planningQuestion,
    empty: true,
    capabilityIds: ["london-planning-activity"],
    refreshProfiles: ["planning-activity-monthly"],
    responses: () => [
      tool("describe_market_data", {}),
      tool("query_market_data", planningArgs),
      toolAfterResult("finalize_market_brief", () => ({ title: "Planning unavailable", status: "partial", facts: [], inferences: [], limitations: [] })),
      finishText(),
    ],
  });

  assertCalls(fixture, ["describe_market_data", "query_market_data", "finalize_market_brief"]);
  const brief = artifact(fixture.outcome);
  assert.equal(brief.status, "unavailable");
  assert.deepEqual(brief.facts, []);
  assert.deepEqual(brief.sources, []);
  assert.deepEqual(brief.lineage, {});
  assert.ok(brief.limitations.some((limitation) => limitation.includes("No canonical planning-application activity")));
});

function record(value: unknown): Readonly<Record<string, unknown>> {
  assert.ok(typeof value === "object" && value !== null && !Array.isArray(value));
  return value as Readonly<Record<string, unknown>>;
}
