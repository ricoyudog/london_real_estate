import assert from "node:assert/strict";
import test from "node:test";

import { DraftRejected, finalizeBrief, type MarketBriefDraftV1 } from "../src/finalizer.ts";
import { TurnContext, defaultTurnLimits } from "../src/runtime.ts";

const session = {
  principal: "principal",
  capability_scope_id: "scope_a",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current"],
  allowed_refresh_profiles: ["bank-rate-latest"],
};

const turnWithCitation = (overrides: Record<string, unknown> = {}) => {
  const turn = new TurnContext(session, defaultTurnLimits);
  turn.addLedgerEntry({
    kind: "citation",
    anchor_as_of: "2026-08-02T00:00:00Z",
    observation_ids: ["observation-1"],
    citation_refs: ["citation-1"],
    numeric_projection: {
      value: "5.25",
      unit: "percent",
      definition: "Bank Rate",
      as_of: "2026-08-01",
      source_date: "2026-08-01",
      period_label: "Current",
      published_at: null,
      datasource_confidence: "high",
      source: "Bank of England",
      ...overrides,
    },
  });
  return turn;
};

const validDraft = (): MarketBriefDraftV1 => ({
  schema_version: "market_brief_draft.v1",
  title: "Bank Rate Market Brief",
  status: "complete",
  facts: [
    { claim_id: "rate", kind: "numeric", confidence: "medium", numeric_citation_ref: "citation-1" },
    {
      claim_id: "context",
      kind: "qualitative",
      confidence: "low",
      text: "Demand remains resilient.",
      supporting_citation_refs: ["citation-1"],
    },
  ],
  inferences: [
    {
      claim_id: "outlook",
      text: "Conditions may remain stable.",
      confidence: "low",
      supporting_fact_ids: ["rate", "context"],
      caveat: "This is an inference.",
    },
  ],
  limitations: ["Coverage remains limited."],
});

const rejected = (draft: unknown, code: string) => {
  assert.throws(
    () => finalizeBrief(draft, turnWithCitation()),
    (error: unknown) => error instanceof DraftRejected && error.code === code,
  );
};

test("finalizeBrief hydrates a valid draft exclusively from the ledger", () => {
  // Given: a valid bounded draft and resolved ledger citation
  const draft = validDraft();

  // When: the host finalizes it
  const brief = finalizeBrief(draft, turnWithCitation());

  // Then: numeric values and host fields originate in the ledger
  assert.equal(brief.schema_version, "market_brief.v1");
  assert.equal(brief.facts[0]?.numeric_value, "5.25");
  assert.equal(brief.as_of, "2026-08-02T00:00:00Z");
  assert.equal(brief.publication_date_warning, true);
  assert.match(brief.display_text, /publication date/i);
});

test("finalizeBrief rejects numeric model values", () => {
  // Given: a numeric fact with a prohibited value field
  const draft = { ...validDraft(), facts: [{ ...validDraft().facts[0], value: "5.25" }] };
  // When/Then: schema escape is rejected
  rejected(draft, "SCHEMA_ESCAPE");
});

test("finalizeBrief rejects unknown inference fact IDs", () => {
  // Given: an inference referring to a missing fact
  const baseline = validDraft();
  const inference = baseline.inferences.at(0);
  assert.ok(inference !== undefined);
  const draft = { ...baseline, inferences: [{ ...inference, supporting_fact_ids: ["missing"] }] };
  // When/Then: lineage validation rejects it
  rejected(draft, "UNKNOWN_FACT_ID");
});

test("finalizeBrief rejects an inference without a caveat", () => {
  // Given: a caveat-free inference
  const baseline = validDraft();
  const inference = baseline.inferences.at(0);
  assert.ok(inference !== undefined);
  const draft = { ...baseline, inferences: [{ claim_id: inference.claim_id, text: inference.text, confidence: inference.confidence, supporting_fact_ids: inference.supporting_fact_ids }] };
  // When/Then: required inference caution is enforced
  rejected(draft, "MISSING_CAVEAT");
});

test("finalizeBrief rejects duplicate claim IDs", () => {
  // Given: a fact and inference sharing a claim ID
  const baseline = validDraft();
  const inference = baseline.inferences.at(0);
  assert.ok(inference !== undefined);
  const draft = { ...baseline, inferences: [{ ...inference, claim_id: "rate" }] };
  // When/Then: the claim namespace remains unique
  rejected(draft, "DUPLICATE_CLAIM_ID");
});

test("finalizeBrief rejects over-limit facts and inferences", () => {
  // Given: drafts beyond each bounded collection limit
  const factDraft = { ...validDraft(), facts: Array.from({ length: 13 }, (_, index) => ({
    claim_id: `f${index}`,
    kind: "numeric" as const,
    confidence: "high" as const,
    numeric_citation_ref: "citation-1",
  })) };
  const inferenceDraft = { ...validDraft(), inferences: Array.from({ length: 9 }, (_, index) => ({
    claim_id: `i${index}`,
    text: "Conditions may change.",
    confidence: "low" as const,
    supporting_fact_ids: ["rate"],
    caveat: "This is uncertain.",
  })) };
  // When/Then: each maximum is independently enforced
  rejected(factDraft, "FACT_LIMIT_EXCEEDED");
  rejected(inferenceDraft, "INFERENCE_LIMIT_EXCEEDED");
});

test("finalizeBrief rejects unknown, cross-anchor, and unresolved citations", () => {
  // Given: three invalid citation lineage configurations
  const baseline = validDraft();
  const fact = baseline.facts.at(0);
  assert.ok(fact !== undefined && fact.kind === "numeric");
  const unknown = { ...baseline, facts: [{ ...fact, numeric_citation_ref: "other-session" }, ...baseline.facts.slice(1)] };
  const crossAnchor = validDraft();
  const unresolvedTurn = turnWithCitation({ value: undefined });
  const crossAnchorTurn = turnWithCitation({ anchor_as_of: "other-anchor" });
  // When/Then: host lineage constraints reject each one
  assert.throws(() => finalizeBrief(unknown, turnWithCitation()), DraftRejected);
  assert.throws(() => finalizeBrief(crossAnchor, crossAnchorTurn), DraftRejected);
  assert.throws(() => finalizeBrief(validDraft(), unresolvedTurn), DraftRejected);
});

test("finalizeBrief guards every model-authored text field", () => {
  // Given: a draft title carrying a number-word bypass
  const draft = { ...validDraft(), title: "five rate brief" };
  // When/Then: finalization rejects unsafe model text
  rejected(draft, "NUMERIC_GUARD_VIOLATION");
});

test("finalizeBrief retains separately sourced confidence fields", () => {
  // Given: a draft whose fact and inference confidence differ from the source
  const brief = finalizeBrief(validDraft(), turnWithCitation({ datasource_confidence: "high" }));
  // When/Then: confidence meanings remain separate
  assert.equal(brief.datasource_confidence["rate"], "high");
  assert.equal(brief.fact_confidence["rate"], "medium");
  assert.equal(brief.inference_confidence["outlook"], "low");
});
