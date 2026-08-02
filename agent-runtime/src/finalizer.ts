// allow: SIZE_OK - the runtime-only finalization security boundary is intentionally co-located.
import type { LedgerEntry, TurnContext } from "./runtime.ts";

const statuses = ["complete", "partial", "unavailable"] as const;
const confidences = ["high", "medium", "low"] as const;
const factKinds = ["numeric", "qualitative"] as const;
const numberWords = new RegExp(
  String.raw`\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth|thirtieth|fortieth|fiftieth|sixtieth|seventieth|eightieth|ninetieth|hundredth|point|percent|gbp|usd|eur|jpy|cny|hkd)\b`,
  "iu",
);
const chineseNumeralCharacters = "零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億";
const chineseNumericExpression = new RegExp(
  String.raw`(?:百分之[${chineseNumeralCharacters}]+|第[${chineseNumeralCharacters}]+|[${chineseNumeralCharacters}]+(?:個)?(?:百分(?:點)?|成|點|元|英鎊|美元|港元|歐元|年|月|日|季|週|周|號|折)|(?<![\p{Script=Han}])[${chineseNumeralCharacters}]+(?![\p{Script=Han}]))`,
  "u",
);
const unicodeNumber = /\p{N}/u;
const percentOrCurrency = /[%％٪\p{Sc}]/u;

type Status = (typeof statuses)[number];
type Confidence = (typeof confidences)[number];
type FactKind = (typeof factKinds)[number];
type UnknownRecord = Record<string, unknown>;

export type NumericFactDraft = {
  readonly claim_id: string;
  readonly kind: "numeric";
  readonly confidence: Confidence;
  readonly numeric_citation_ref: string;
};

export type QualitativeFactDraft = {
  readonly claim_id: string;
  readonly kind: "qualitative";
  readonly confidence: Confidence;
  readonly text: string;
  readonly supporting_citation_refs: readonly string[];
};

export type InferenceDraft = {
  readonly claim_id: string;
  readonly text: string;
  readonly confidence: Confidence;
  readonly supporting_fact_ids: readonly string[];
  readonly caveat: string;
};

export type MarketBriefDraftV1 = {
  readonly schema_version: "market_brief_draft.v1";
  readonly title: string;
  readonly status: Status;
  readonly facts: readonly (NumericFactDraft | QualitativeFactDraft)[];
  readonly inferences: readonly InferenceDraft[];
  readonly limitations: readonly string[];
};

type NumericProjection = {
  readonly value: string;
  readonly unit: string;
  readonly definition: string;
  readonly as_of: string;
  readonly source_date: string;
  readonly period_label: string;
  readonly published_at: string | null;
  readonly datasource_confidence: Confidence;
  readonly source: string;
  readonly public_url: string | null;
  readonly anchor_as_of?: string;
};

type NumericFields = Pick<NumericProjection, "value" | "unit" | "definition" | "as_of" | "source_date" | "period_label">;
type CitationMetadata = Pick<NumericProjection, "published_at" | "datasource_confidence" | "source" | "public_url" | "anchor_as_of">;

type CitationResolution = {
  readonly ref: string;
  readonly entry: LedgerEntry;
  readonly projection: NumericProjection;
};

export type HostLedger = {
  readonly anchor_as_of: string | null;
  readonly citations: ReadonlyMap<string, CitationResolution>;
};

export type HydratedFact = {
  readonly claim_id: string;
  readonly kind: FactKind;
  readonly confidence: Confidence;
  readonly text?: string;
  readonly supporting_citation_refs?: readonly string[];
  readonly numeric_value?: string;
  readonly numeric_unit?: string;
  readonly numeric_definition?: string;
  readonly numeric_as_of?: string;
  readonly numeric_source_date?: string;
  readonly numeric_period_label?: string;
};

export type MarketBriefV1 = {
  readonly schema_version: "market_brief.v1";
  readonly title: string;
  readonly status: Status;
  readonly facts: readonly HydratedFact[];
  readonly inferences: readonly InferenceDraft[];
  readonly limitations: readonly string[];
  readonly as_of: string | null;
  readonly sources: readonly { readonly citation_ref: string; readonly source: string; readonly public_url: string | null; readonly published_at: string | null }[];
  readonly lineage: Readonly<Record<string, { readonly observation_ids: readonly string[]; readonly citation_refs: readonly string[] }>>;
  readonly freshness_warnings: readonly string[];
  readonly published_at: string | null;
  readonly publication_date_warning: boolean;
  readonly datasource_confidence: Readonly<Record<string, Confidence>>;
  readonly fact_confidence: Readonly<Record<string, Confidence>>;
  readonly inference_confidence: Readonly<Record<string, Confidence>>;
  readonly display_text: string;
};

export class DraftRejected extends Error {
  readonly name = "DraftRejected";
  readonly code: string;
  readonly offending_ref: string | undefined;

  constructor(code: string, offending_ref?: string) {
    super(offending_ref === undefined ? `draft rejected: ${code}` : `draft rejected: ${code}: ${offending_ref}`);
    this.code = code;
    this.offending_ref = offending_ref;
  }
}

export class NumericGuardViolation extends Error {
  readonly name = "NumericGuardViolation";
  readonly span: string;

  constructor(span: string) {
    super(`numeric guard violation: ${span}`);
    this.span = span;
  }
}

export class NumericGuard {
  static check(text: string): string | null {
    const normalized = text.normalize("NFKC");
    const unicodeMatch = normalized.match(unicodeNumber);
    if (unicodeMatch?.[0] !== undefined) return unicodeMatch[0];
    const tokenMatch = normalized.match(percentOrCurrency);
    if (tokenMatch?.[0] !== undefined) return tokenMatch[0];
    const wordMatch = normalized.match(numberWords);
    if (wordMatch?.[0] !== undefined) return wordMatch[0];
    const chineseMatch = normalized.match(chineseNumericExpression);
    return chineseMatch?.[0] ?? null;
  }
}

export class ModelTextBuffer {
  #text = "";
  #checkedThrough = 0;
  #safe = false;
  guardRejected = false;

  append(chunk: string): void {
    if (this.guardRejected) throw new NumericGuardViolation("buffer closed");
    if (this.#safe) throw new NumericGuardViolation("buffer already flushed");
    this.#text += chunk;
    this.#guardCompleted();
  }

  flush(): string {
    if (this.guardRejected) throw new NumericGuardViolation("buffer closed");
    this.#guard();
    this.#safe = true;
    return this.#text;
  }

  #guard(): void {
    const span = NumericGuard.check(this.#text);
    if (span === null) return;
    this.guardRejected = true;
    throw new NumericGuardViolation(span);
  }

  #guardCompleted(): void {
    const boundaries = [...this.#text.matchAll(/[\s!?;:%％٪\p{Sc}]/gu)];
    const lastBoundary = boundaries.at(-1);
    if (lastBoundary?.index === undefined) return;
    const completedThrough = lastBoundary.index + lastBoundary[0].length;
    const completed = this.#text.slice(this.#checkedThrough, completedThrough);
    const span = NumericGuard.check(completed);
    if (span !== null) {
      this.guardRejected = true;
      throw new NumericGuardViolation(span);
    }
    this.#checkedThrough = completedThrough;
  }
}

export const hostLedger = (turn: TurnContext): HostLedger => {
  const numeric = new Map<string, { readonly entry: LedgerEntry; readonly fields: NumericFields }>();
  const metadata = new Map<string, CitationMetadata>();
  let anchor_as_of: string | null = null;
  for (const entry of turn.getLedger()) {
    if (anchor_as_of === null) anchor_as_of = entry.anchor_as_of;
    if (anchor_as_of !== entry.anchor_as_of) throw new DraftRejected("CROSS_ANCHOR");
    for (const ref of entry.citation_refs) {
      const numericFields = parseNumericFields(entry.numeric_projection);
      const citationMetadata = parseCitationMetadata(entry.numeric_projection);
      if (numericFields !== null) {
        if (numeric.has(ref)) throw new DraftRejected("DUPLICATE_CITATION_REF", ref);
        numeric.set(ref, { entry, fields: numericFields });
      }
      if (citationMetadata !== null) {
        if (metadata.has(ref)) throw new DraftRejected("DUPLICATE_CITATION_REF", ref);
        if (citationMetadata.anchor_as_of !== undefined && citationMetadata.anchor_as_of !== entry.anchor_as_of) throw new DraftRejected("CROSS_ANCHOR", ref);
        metadata.set(ref, citationMetadata);
      }
    }
  }
  const citations = new Map<string, CitationResolution>();
  for (const [ref, resolution] of numeric) {
    const citationMetadata = metadata.get(ref);
    if (citationMetadata !== undefined) citations.set(ref, { ref, entry: resolution.entry, projection: { ...resolution.fields, ...citationMetadata } });
  }
  return { anchor_as_of, citations };
};

export const finalizeBrief = (input: unknown, turn: TurnContext): MarketBriefV1 => {
  const draft = parseDraft(input);
  guardModelText(draft);
  const ledger = hostLedger(turn);
  if (ledger.anchor_as_of === null && draft.facts.length > 0) throw new DraftRejected("UNRESOLVED_REF");
  const references = resolveReferences(draft, ledger);
  const sources = [...references.values()].map(({ ref, projection }) => ({
    citation_ref: ref,
    source: projection.source,
    public_url: projection.public_url,
    published_at: projection.published_at,
  }));
  const freshness_warnings = freshnessWarnings(references);
  const published_at = sources.find((source) => source.published_at !== null)?.published_at ?? null;
  const publication_date_warning = published_at === null;
  const factConfidence: Record<string, Confidence> = {};
  const datasourceConfidence: Record<string, Confidence> = {};
  const inferenceConfidence: Record<string, Confidence> = {};
  const lineage: Record<string, { readonly observation_ids: readonly string[]; readonly citation_refs: readonly string[] }> = {};
  const facts = draft.facts.map((fact) => {
    factConfidence[fact.claim_id] = fact.confidence;
    const factRefs = fact.kind === "numeric" ? [fact.numeric_citation_ref] : fact.supporting_citation_refs;
    const resolution = requiredResolution(references, factRefs[0]);
    datasourceConfidence[fact.claim_id] = resolution.projection.datasource_confidence;
    lineage[fact.claim_id] = {
      observation_ids: [...new Set(factRefs.flatMap((ref) => requiredResolution(references, ref).entry.observation_ids))],
      citation_refs: factRefs,
    };
    if (fact.kind === "qualitative") return fact;
    return {
      claim_id: fact.claim_id,
      kind: fact.kind,
      confidence: fact.confidence,
      numeric_value: resolution.projection.value,
      numeric_unit: resolution.projection.unit,
      numeric_definition: resolution.projection.definition,
      numeric_as_of: resolution.projection.as_of,
      numeric_source_date: resolution.projection.source_date,
      numeric_period_label: resolution.projection.period_label,
    };
  });
  for (const inference of draft.inferences) inferenceConfidence[inference.claim_id] = inference.confidence;
  return {
    schema_version: "market_brief.v1",
    title: draft.title,
    status: draft.status === "complete" && freshness_warnings.length > 0 ? "partial" : draft.status,
    facts,
    inferences: draft.inferences,
    limitations: draft.limitations,
    as_of: ledger.anchor_as_of,
    sources,
    lineage,
    freshness_warnings,
    published_at,
    publication_date_warning,
    datasource_confidence: datasourceConfidence,
    fact_confidence: factConfidence,
    inference_confidence: inferenceConfidence,
    display_text: renderDisplayText(draft.title, publication_date_warning, draft.facts.length === 0),
  };
};

const parseDraft = (input: unknown): MarketBriefDraftV1 => {
  const record = requireRecord(input);
  requireKeys(record, ["schema_version", "title", "status", "facts", "inferences", "limitations"]);
  if (record.schema_version !== "market_brief_draft.v1") rejectSchema();
  const title = requireString(record.title);
  const status = requireMember(record.status, statuses);
  const factsInput = requireArray(record.facts);
  const inferencesInput = requireArray(record.inferences);
  const limitations = requireStrings(record.limitations);
  if (factsInput.length > 12) throw new DraftRejected("FACT_LIMIT_EXCEEDED");
  if (inferencesInput.length > 8) throw new DraftRejected("INFERENCE_LIMIT_EXCEEDED");
  const facts = factsInput.map(parseFact);
  const inferences = inferencesInput.map(parseInference);
  const ids = new Set<string>();
  for (const claim of [...facts, ...inferences]) {
    if (ids.has(claim.claim_id)) throw new DraftRejected("DUPLICATE_CLAIM_ID", claim.claim_id);
    ids.add(claim.claim_id);
  }
  for (const inference of inferences) {
    for (const factId of inference.supporting_fact_ids) {
      if (!facts.some((fact) => fact.claim_id === factId)) throw new DraftRejected("UNKNOWN_FACT_ID", factId);
    }
  }
  return { schema_version: "market_brief_draft.v1", title, status, facts, inferences, limitations };
};

const parseFact = (input: unknown): NumericFactDraft | QualitativeFactDraft => {
  const record = requireRecord(input);
  const kind = requireMember(record.kind, factKinds);
  const claimId = requireString(record.claim_id);
  const confidence = requireMember(record.confidence, confidences);
  switch (kind) {
    case "numeric":
      requireKeys(record, ["claim_id", "kind", "confidence", "numeric_citation_ref"]);
      const numericCitationRef = requireString(record.numeric_citation_ref);
      return { claim_id: claimId, kind, confidence, numeric_citation_ref: numericCitationRef };
    case "qualitative":
      requireKeys(record, ["claim_id", "kind", "confidence", "text", "supporting_citation_refs"]);
      const text = requireString(record.text);
      const supportingCitationRefs = requireStrings(record.supporting_citation_refs);
      return { claim_id: claimId, kind, confidence, text, supporting_citation_refs: supportingCitationRefs };
  }
  return rejectSchema();
};

const parseInference = (input: unknown): InferenceDraft => {
  const record = requireRecord(input);
  if (!Object.hasOwn(record, "caveat")) throw new DraftRejected("MISSING_CAVEAT");
  requireKeys(record, ["claim_id", "text", "confidence", "supporting_fact_ids", "caveat"]);
  const claimId = requireString(record.claim_id);
  const text = requireString(record.text);
  const confidence = requireMember(record.confidence, confidences);
  const factIds = requireStrings(record.supporting_fact_ids);
  const caveat = requireString(record.caveat);
  return { claim_id: claimId, text, confidence, supporting_fact_ids: factIds, caveat };
};

const resolveReferences = (draft: MarketBriefDraftV1, ledger: HostLedger): ReadonlyMap<string, CitationResolution> => {
  for (const fact of draft.facts) {
    const factRefs = fact.kind === "numeric" ? [fact.numeric_citation_ref] : fact.supporting_citation_refs;
    const refs = new Set<string>();
    for (const ref of factRefs) {
      if (refs.has(ref)) throw new DraftRejected("DUPLICATE_CITATION_REF", ref);
      refs.add(ref);
    }
  }
  const resolved = new Map<string, CitationResolution>();
  for (const ref of [...draft.facts.flatMap((fact) => fact.kind === "numeric" ? [fact.numeric_citation_ref] : fact.supporting_citation_refs)]) {
    const resolution = ledger.citations.get(ref);
    if (resolution === undefined) throw new DraftRejected("UNKNOWN_CITATION_REF", ref);
    if (resolution.projection.anchor_as_of !== undefined && resolution.projection.anchor_as_of !== ledger.anchor_as_of) {
      throw new DraftRejected("CROSS_ANCHOR", ref);
    }
    resolved.set(ref, resolution);
  }
  return resolved;
};

const guardModelText = (draft: MarketBriefDraftV1): void => {
  const text = [
    draft.title,
    ...draft.limitations,
    ...draft.facts.flatMap((fact) => (fact.kind === "qualitative" ? [fact.text] : [])),
    ...draft.inferences.flatMap((inference) => [inference.text, inference.caveat]),
  ];
  for (const value of text) {
    const span = NumericGuard.check(value);
    if (span !== null) throw new DraftRejected("NUMERIC_GUARD_VIOLATION", span);
  }
};

const parseNumericFields = (input: unknown): NumericFields | null => {
  if (!isRecord(input)) return null;
  const value = input.value;
  const unit = input.unit;
  const definition = input.definition;
  const asOf = input.as_of;
  const sourceDate = input.source_date;
  const periodLabel = input.period_label;
  if (typeof value !== "string" || typeof unit !== "string" || typeof definition !== "string" || typeof asOf !== "string" || typeof sourceDate !== "string" || typeof periodLabel !== "string") return null;
  return { value, unit, definition, as_of: asOf, source_date: sourceDate, period_label: periodLabel };
};

const parseCitationMetadata = (input: unknown): CitationMetadata | null => {
  if (!isRecord(input)) return null;
  const publishedAt = input.published_at;
  const confidence = input.datasource_confidence;
  const source = input.source;
  const publicUrl = input.public_url;
  const anchorAsOf = input.anchor_as_of;
  if (publishedAt !== null && typeof publishedAt !== "string") return null;
  if (!isMember(confidence, confidences) || typeof source !== "string") return null;
  if (publicUrl !== undefined && publicUrl !== null && typeof publicUrl !== "string") return null;
  if (anchorAsOf !== undefined && typeof anchorAsOf !== "string") return null;
  return { published_at: publishedAt, datasource_confidence: confidence, source, public_url: typeof publicUrl === "string" ? publicUrl : null, ...(anchorAsOf === undefined ? {} : { anchor_as_of: anchorAsOf }) };
};

const freshnessWarnings = (references: ReadonlyMap<string, CitationResolution>): readonly string[] => {
  let stale = false;
  let degraded = false;
  for (const { entry } of references.values()) {
    if (!isRecord(entry.freshness)) continue;
    stale ||= entry.freshness.retrieval_freshness === "stale" || entry.freshness.observation_freshness === "stale";
    degraded ||= entry.freshness.degraded === true;
  }
  return [
    ...(stale ? ["Canonical data is stale; the last-good value is retained."] : []),
    ...(degraded ? ["Canonical data is degraded; verify freshness before relying on it."] : []),
  ];
};

const requireRecord = (input: unknown): UnknownRecord => {
  if (isRecord(input)) return input;
  return rejectSchema();
};

const requireKeys = (record: UnknownRecord, allowed: readonly string[]): void => {
  if (Object.keys(record).some((key) => !allowed.includes(key))) rejectSchema();
};

function isRecord(input: unknown): input is UnknownRecord {
  return typeof input === "object" && input !== null && !Array.isArray(input);
}

function isStringArray(input: unknown): input is readonly string[] {
  return Array.isArray(input) && input.every((value) => typeof value === "string");
}

const requireArray = (input: unknown): readonly unknown[] => Array.isArray(input) ? input : rejectSchema();
const requireStrings = (input: unknown): readonly string[] => isStringArray(input) ? input : rejectSchema();
const requireString = (input: unknown): string => typeof input === "string" ? input : rejectSchema();
const requireMember = <T extends readonly string[]>(input: unknown, values: T): T[number] => isMember(input, values) ? input : rejectSchema();

function isMember<T extends readonly string[]>(input: unknown, values: T): input is T[number] {
  return typeof input === "string" && values.includes(input);
}
const rejectSchema = (): never => { throw new DraftRejected("SCHEMA_ESCAPE"); };
const requiredResolution = (references: ReadonlyMap<string, CitationResolution>, ref: string | undefined): CitationResolution => {
  if (ref === undefined) throw new DraftRejected("UNRESOLVED_REF");
  const resolution = references.get(ref);
  if (resolution === undefined) throw new DraftRejected("UNRESOLVED_REF", ref);
  return resolution;
};
const renderDisplayText = (title: string, publicationDateWarning: boolean, coverageUnavailable: boolean): string => {
  if (coverageUnavailable) return `${title}. Coverage unavailable.`;
  return publicationDateWarning ? `${title}. Publication date unavailable.` : title;
};
