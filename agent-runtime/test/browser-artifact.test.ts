import assert from "node:assert/strict";
import test from "node:test";

import { projectArtifactForBrowser } from "../src/browser-artifact.ts";

test("browser artifact projection replaces signed citation handles with stable source aliases", () => {
  const projected = record(projectArtifactForBrowser({
    schema_version: "market_brief.v1",
    title: "Bank Rate brief",
    status: "complete",
    facts: [
      { claim_id: "rate", kind: "numeric", confidence: "high", numeric_value: "5.25", numeric_unit: "percent", numeric_definition: "Bank Rate", numeric_as_of: "2026-08-03T00:00:00Z", numeric_source_date: "2026-08-01", numeric_period_label: "1 Aug 2026" },
      { claim_id: "publisher", kind: "qualitative", confidence: "high", text: "Published by the Bank of England.", supporting_citation_refs: ["h1.first-signed-handle", "h1.second-signed-handle"] },
    ],
    inferences: [{ claim_id: "outlook", text: "Conditions may change.", confidence: "low", supporting_fact_ids: ["rate"], caveat: "Future decisions may differ." }],
    limitations: [],
    as_of: "2026-08-03T00:00:00Z",
    sources: [
      { citation_ref: "h1.first-signed-handle", source: "Bank of England", public_url: "https://example.test/rate", published_at: "2026-08-01T09:00:00Z" },
      { citation_ref: "h1.second-signed-handle", source: "Bank of England archive", public_url: null, published_at: null },
    ],
    lineage: {
      rate: { observation_ids: ["observation-1"], citation_refs: ["h1.first-signed-handle"] },
      publisher: { observation_ids: ["observation-1"], citation_refs: ["h1.first-signed-handle", "h1.second-signed-handle"] },
    },
    freshness_warnings: [],
    published_at: "2026-08-01T09:00:00Z",
    publication_date_warning: false,
    datasource_confidence: { rate: "high", publisher: "high" },
    fact_confidence: { rate: "high", publisher: "high" },
    inference_confidence: { outlook: "low" },
    display_text: "Bank Rate brief",
    unexpected: "h1.unlisted-secret",
  }));

  assert.equal(JSON.stringify(projected).includes("h1."), false);
  assert.equal("unexpected" in projected, false);
  assert.deepEqual(array(projected["sources"]).map((value) => record(value)["citation_ref"]), ["source-1", "source-2"]);
  assert.deepEqual(array(projected["sources"]).map((value) => record(value)["source_alias"]), ["Source 1", "Source 2"]);
  assert.deepEqual(record(array(projected["facts"])[1])["supporting_citation_refs"], ["source-1", "source-2"]);
  assert.deepEqual(record(record(projected["lineage"])["publisher"])["citation_refs"], ["source-1", "source-2"]);
});

test("browser artifact projection fails closed for unknown or malformed artifacts", () => {
  const unknown = projectArtifactForBrowser({ schema_version: "other.v1", secret: "h1.unknown-handle" });
  const malformed = projectArtifactForBrowser({ schema_version: "market_brief.v1", title: "Bad", sources: [{ citation_ref: 42 }] });

  for (const value of [unknown, malformed]) {
    assert.deepEqual(value, {
      schema_version: "unsupported_artifact.v1",
      message: "The host returned an unsupported final artifact.",
    });
    assert.equal(JSON.stringify(value).includes("h1."), false);
  }
});

function record(value: unknown): Readonly<Record<string, unknown>> {
  assert.ok(typeof value === "object" && value !== null && !Array.isArray(value));
  return value as Readonly<Record<string, unknown>>;
}

function array(value: unknown): readonly unknown[] {
  assert.ok(Array.isArray(value));
  return value;
}
