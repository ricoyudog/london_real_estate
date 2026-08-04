import assert from "node:assert/strict";
import test from "node:test";

import { DraftRejected, finalizeBrief, type MarketBriefDraftV1 } from "../src/finalizer.ts";
import { TurnContext, defaultTurnLimits } from "../src/runtime.ts";

const planningLimitation = "Planning application counts cover all use classes and are not office-only supply, floorspace, completions, rent, vacancy, or named-submarket evidence.";
const planningAvailabilityLimitation = "No canonical planning-application activity is available for the requested borough and month.";

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

const planningTurn = (): TurnContext => {
  const turn = new TurnContext(session, defaultTurnLimits);
  turn.addLedgerEntry({
    kind: "query", anchor_as_of: "2026-08-02T00:00:00Z", capability_id: "london-planning-activity",
    observation_ids: ["planning-observation"], citation_refs: ["planning-citation"],
    numeric_projection: { value: "2", unit: "applications", definition: "Planning application count", as_of: "2026-08-01", source_date: "2026-07-31", period_label: "July 2026" },
  });
  turn.addLedgerEntry({
    kind: "citation", anchor_as_of: "2026-08-02T00:00:00Z", capability_id: "london-planning-activity",
    observation_ids: ["planning-observation"], citation_refs: ["planning-citation"],
    numeric_projection: { published_at: "2026-08-01T00:00:00Z", datasource_confidence: "high", source: "Planning Data", public_url: "https://files.planning.data.gov.uk" },
  });
  return turn;
};

const capabilityTurn = (capabilityId: string): TurnContext => {
  const turn = new TurnContext(session, defaultTurnLimits);
  turn.addLedgerEntry({
    kind: "query", anchor_as_of: "2026-08-02T00:00:00Z", capability_id: capabilityId,
    observation_ids: ["capability-observation"], citation_refs: ["capability-citation"],
    numeric_projection: { value: "1", unit: "index", definition: "Capability measure", as_of: "2026-08-01", source_date: "2026-08-01", period_label: "Current" },
  });
  turn.addLedgerEntry({
    kind: "citation", anchor_as_of: "2026-08-02T00:00:00Z", capability_id: capabilityId,
    observation_ids: ["capability-observation"], citation_refs: ["capability-citation"],
    numeric_projection: { published_at: null, datasource_confidence: "high", source: "Official source", public_url: null },
  });
  return turn;
};

const capabilityDraft = (title: string): MarketBriefDraftV1 => ({
  ...validDraft(), title, facts: [{ claim_id: "capability", kind: "numeric", confidence: "high", numeric_citation_ref: "capability-citation" }], inferences: [], limitations: [],
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

test("finalizeBrief keeps numeric and qualitative citations on their own record lineage", () => {
  // Given: two query records with distinct values and per-citation metadata
  const turn = new TurnContext(session, defaultTurnLimits);
  for (const [observation, citation, value] of [["observation-a", "citation-a", "5.25"], ["observation-b", "citation-b", "6.50"]] as const) {
    turn.addLedgerEntry({
      kind: "query", anchor_as_of: "2026-08-02T00:00:00Z", observation_ids: [observation, "observation-shared"], citation_refs: [citation],
      numeric_projection: { value, unit: "percent", definition: `Bank Rate ${value}`, as_of: "2026-08-01", source_date: "2026-08-01", period_label: value },
    });
    turn.addLedgerEntry({
      kind: "citation", anchor_as_of: "2026-08-02T00:00:00Z", observation_ids: [observation, "observation-shared"], citation_refs: [citation],
      numeric_projection: { published_at: `${value}Z`, datasource_confidence: value === "6.50" ? "medium" : "high", source: `Publisher ${value}` },
    });
  }
  const draft: MarketBriefDraftV1 = {
    schema_version: "market_brief_draft.v1", title: "Lineage", status: "complete",
    facts: [
      { claim_id: "numeric-b", kind: "numeric", confidence: "high", numeric_citation_ref: "citation-b" },
      { claim_id: "qualitative", kind: "qualitative", confidence: "medium", text: "Sources differ.", supporting_citation_refs: ["citation-a", "citation-b"] },
    ],
    inferences: [], limitations: [],
  };

  // When: the host hydrates the draft
  const brief = finalizeBrief(draft, turn);

  // Then: record B keeps its own numeric value and both refs remain distinct
  assert.equal(brief.facts[0]?.numeric_value, "6.50");
  assert.equal(brief.facts[0]?.numeric_definition, "Bank Rate 6.50");
  assert.deepEqual(brief.lineage["numeric-b"]?.observation_ids, ["observation-b", "observation-shared"]);
  assert.deepEqual(brief.lineage["qualitative"]?.citation_refs, ["citation-a", "citation-b"]);
  assert.deepEqual(brief.lineage["qualitative"]?.observation_ids, ["observation-a", "observation-shared", "observation-b"]);
  assert.deepEqual(brief.sources.map((source) => source.source), ["Publisher 6.50", "Publisher 5.25"]);
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

test("finalizeBrief injects the host planning limitation for planning-backed facts", () => {
  // Given: a fact resolved only through an authoritative planning query ledger entry
  const draft: MarketBriefDraftV1 = { ...validDraft(), facts: [{ claim_id: "planning", kind: "numeric", confidence: "high", numeric_citation_ref: "planning-citation" }], inferences: [], limitations: [] };

  // When: the host finalizes the planning brief
  const brief = finalizeBrief(draft, planningTurn());

  // Then: the fixed proxy limitation is added by the host
  assert.ok(brief.limitations.includes(planningLimitation));
});

test("finalizeBrief makes an attempted zero-record planning query unavailable without lineage", () => {
  // Given: host metadata for an empty planning attempt
  const turn = new TurnContext(session, defaultTurnLimits);
  turn.addLedgerEntry({ kind: "query", anchor_as_of: "2026-08-02T00:00:00Z", capability_id: "london-planning-activity", observation_ids: [], citation_refs: [] });
  const draft: MarketBriefDraftV1 = { schema_version: "market_brief_draft.v1", title: "Planning coverage", status: "partial", facts: [], inferences: [], limitations: [] };

  // When: the model submits an empty brief
  const brief = finalizeBrief(draft, turn);

  // Then: canonical availability is host-authored and does not invent evidence
  assert.equal(brief.status, "unavailable");
  assert.deepEqual(brief.facts, []);
  assert.deepEqual(brief.sources, []);
  assert.deepEqual(brief.lineage, {});
  assert.deepEqual(brief.limitations, [planningAvailabilityLimitation]);
});

test("finalizeBrief does not let a zero-record planning attempt relabel a Bank Rate fact", () => {
  // Given: an earlier empty planning query and a later Bank Rate citation
  const turn = planningTurn();
  turn.addLedgerEntry({ kind: "query", anchor_as_of: "2026-08-02T00:00:00Z", capability_id: "london-planning-activity", observation_ids: [], citation_refs: [] });
  turn.addLedgerEntry({ kind: "query", anchor_as_of: "2026-08-02T00:00:00Z", capability_id: "uk.bank-rate-current", observation_ids: ["rate-observation"], citation_refs: ["rate-citation"], numeric_projection: { value: "5.25", unit: "percent", definition: "Bank Rate", as_of: "2026-08-01", source_date: "2026-08-01", period_label: "Current" } });
  turn.addLedgerEntry({ kind: "citation", anchor_as_of: "2026-08-02T00:00:00Z", capability_id: "uk.bank-rate-current", observation_ids: ["rate-observation"], citation_refs: ["rate-citation"], numeric_projection: { published_at: null, datasource_confidence: "high", source: "Bank of England", public_url: null } });
  const draft: MarketBriefDraftV1 = { ...validDraft(), facts: [{ claim_id: "rate", kind: "numeric", confidence: "high", numeric_citation_ref: "rate-citation" }], inferences: [], limitations: [] };

  // When: finalization resolves the Bank Rate citation
  const brief = finalizeBrief(draft, turn);

  // Then: no planning limitation leaks into the Bank Rate artifact
  assert.equal(brief.facts[0]?.numeric_value, "5.25");
  assert.ok(!brief.limitations.includes(planningLimitation));
});

test("finalizeBrief rejects planning claims that mislabel planning evidence", () => {
  const forbidden = ["office supply", "floorspace", "completions", "rent", "vacancy", "Mayfair submarket"] as const;
  const fields = ["title", "qualitative", "inference", "caveat", "limitation"] as const;
  for (const field of fields) for (const claim of forbidden) {
    // Given: a planning-backed draft with an adversarial claim in each model-authored field
    const baseline: MarketBriefDraftV1 = { ...validDraft(), facts: [{ claim_id: "planning", kind: "numeric", confidence: "high", numeric_citation_ref: "planning-citation" }], inferences: [], limitations: [] };
    const draft = field === "title" ? { ...baseline, title: claim }
      : field === "qualitative" ? { ...baseline, facts: [...baseline.facts, { claim_id: "context", kind: "qualitative" as const, confidence: "low" as const, text: claim, supporting_citation_refs: ["planning-citation"] }] }
      : field === "inference" ? { ...baseline, inferences: [{ claim_id: "outlook", text: claim, confidence: "low" as const, supporting_fact_ids: ["planning"], caveat: "Scope is limited." }] }
      : field === "caveat" ? { ...baseline, inferences: [{ claim_id: "outlook", text: "Scope is limited.", confidence: "low" as const, supporting_fact_ids: ["planning"], caveat: claim }] }
      : { ...baseline, limitations: [claim] };

    // When/Then: planning proxy enforcement rejects every forbidden category
    assert.throws(() => finalizeBrief(draft, planningTurn()), (error: unknown) => error instanceof DraftRejected && error.code === "PLANNING_PROXY_CLAIM");
  }
});

test("finalizeBrief accepts model-negated planning scope wording", () => {
  // Given: a planning-backed brief that correctly preserves the limitation
  const draft: MarketBriefDraftV1 = { ...validDraft(), facts: [{ claim_id: "planning", kind: "numeric", confidence: "high", numeric_citation_ref: "planning-citation" }], inferences: [], limitations: ["Includes all use classes, not office-specific."] };

  // When: the host finalizes it
  const brief = finalizeBrief(draft, planningTurn());

  // Then: accurate negated wording remains valid alongside the host limitation
  assert.ok(brief.limitations.includes("Includes all use classes, not office-specific."));
  assert.ok(brief.limitations.includes(planningLimitation));
});

test("finalizeBrief rejects a planning claim that hides a forbidden metric behind another negation", () => {
  // Given: a planning claim with a correct office-supply negation but an incorrect rent claim
  const draft: MarketBriefDraftV1 = { ...validDraft(), facts: [{ claim_id: "planning", kind: "numeric", confidence: "high", numeric_citation_ref: "planning-citation" }], inferences: [], limitations: ["This is not office supply, but it shows rent."] };

  // When/Then: each forbidden metric must be independently negated
  assert.throws(() => finalizeBrief(draft, planningTurn()), (error: unknown) => error instanceof DraftRejected && error.code === "PLANNING_PROXY_CLAIM");
});

test("finalizeBrief rejects capability-specific proxy claims", () => {
  const cases = [
    ["uk.gdp.current", "London office conditions", "GDP_PROXY_CLAIM"],
    ["uk.inflation.current", "office rent outlook", "INFLATION_PROXY_CLAIM"],
    ["uk.labour.current", "office vacancy signal", "LABOUR_PROXY_CLAIM"],
    ["uk.employment.london", "office rent outlook", "EMPLOYMENT_PROXY_CLAIM"],
    ["uk.hybrid-working", "office occupancy trend", "HYBRID_PROXY_CLAIM"],
    ["london.epc-certificates", "office certificates total", "EPC_PROXY_CLAIM"],
    ["london.office-stock", "vacancy measure", "OFFICE_STOCK_PROXY_CLAIM"],
  ] as const;
  for (const [capabilityId, title, code] of cases) {
    // Given: a draft backed by a proxy capability and relabelled in its title
    const draft = capabilityDraft(title);

    // When/Then: the capability-specific guard rejects the relabelling
    assert.throws(() => finalizeBrief(draft, capabilityTurn(capabilityId)), (error: unknown) => error instanceof DraftRejected && error.code === code);
  }
});

test("finalizeBrief accepts negated capability proxy claims", () => {
  const cases = [
    ["uk.gdp.current", "not London office evidence"],
    ["uk.hybrid-working", "not office occupancy evidence"],
    ["london.office-stock", "no vacancy evidence"],
  ] as const;
  for (const [capabilityId, title] of cases) {
    // Given: a capability-backed draft accurately states the prohibited scope is absent
    const draft = capabilityDraft(title);

    // When: the host finalizes it
    const brief = finalizeBrief(draft, capabilityTurn(capabilityId));

    // Then: the negated limitation remains valid
    assert.equal(brief.title, title);
  }
});

test("finalizeBrief projects host source URLs and forces stale or degraded facts to partial", () => {
  const turn = new TurnContext(session, defaultTurnLimits);
  turn.addLedgerEntry({
    kind: "query",
    anchor_as_of: "2026-08-02T00:00:00Z",
    observation_ids: ["observation-1"],
    citation_refs: ["citation-1"],
    numeric_projection: {
      value: "5.25", unit: "percent", definition: "Bank Rate", as_of: "2026-08-01",
      source_date: "2026-08-01", period_label: "Current",
    },
    freshness: { retrieval_freshness: "stale", observation_freshness: "fresh", degraded: true },
  });
  turn.addLedgerEntry({
    kind: "citation",
    anchor_as_of: "2026-08-02T00:00:00Z",
    observation_ids: ["observation-1"],
    citation_refs: ["citation-1"],
    numeric_projection: {
      published_at: "2026-08-01T09:00:00Z", datasource_confidence: "high",
      source: "Bank of England", public_url: "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp",
    },
  });

  const brief = finalizeBrief({ ...validDraft(), facts: [validDraft().facts[0]], inferences: [] }, turn);

  assert.equal(brief.status, "partial");
  assert.deepEqual(brief.freshness_warnings, [
    "Canonical data is stale; the last-good value is retained.",
    "Canonical data is degraded; verify freshness before relying on it.",
  ]);
  assert.equal(brief.sources[0]?.public_url, "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp");
});

test("finalizeBrief completes an unavailable zero-fact brief with an empty ledger", () => {
  // Given: unavailable coverage without canonical facts
  const draft: MarketBriefDraftV1 = { schema_version: "market_brief_draft.v1", title: "Coverage", status: "unavailable", facts: [], inferences: [], limitations: ["No canonical coverage."] };

  // When: the host finalizes against an empty turn ledger
  const brief = finalizeBrief(draft, new TurnContext(session, defaultTurnLimits));

  // Then: the unavailable coverage artifact is preserved without citations
  assert.equal(brief.status, "unavailable");
  assert.deepEqual(brief.sources, []);
  assert.equal(brief.publication_date_warning, true);
  assert.match(brief.display_text, /coverage unavailable/i);
  assert.deepEqual(brief.datasource_confidence, {});
  assert.deepEqual(brief.fact_confidence, {});
  assert.deepEqual(brief.inference_confidence, {});
});

test("finalizeBrief completes a partial zero-fact brief with an empty ledger", () => {
  // Given: partial coverage without canonical facts
  const draft: MarketBriefDraftV1 = { schema_version: "market_brief_draft.v1", title: "Coverage", status: "partial", facts: [], inferences: [], limitations: ["No canonical coverage."] };

  // When: the host finalizes against an empty turn ledger
  const brief = finalizeBrief(draft, new TurnContext(session, defaultTurnLimits));

  // Then: the coverage artifact remains partial without citations
  assert.equal(brief.status, "partial");
  assert.deepEqual(brief.sources, []);
  assert.equal(brief.publication_date_warning, true);
  assert.match(brief.display_text, /coverage unavailable/i);
});

test("finalizeBrief rejects fact-bearing briefs with an empty ledger", () => {
  // Given: a numeric fact with no resolvable ledger anchor
  const baseline = validDraft();
  const fact = baseline.facts.at(0);
  assert.ok(fact !== undefined);
  const draft: MarketBriefDraftV1 = { ...baseline, facts: [fact], inferences: [] };

  // When/Then: fact-bearing drafts retain the unresolved-reference rejection
  assert.throws(() => finalizeBrief(draft, new TurnContext(session, defaultTurnLimits)), (error: unknown) => error instanceof DraftRejected && error.code === "UNRESOLVED_REF");
});
