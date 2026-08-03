const unsupportedArtifact = {
  schema_version: "unsupported_artifact.v1",
  message: "The host returned an unsupported final artifact.",
} as const;

type UnknownRecord = Readonly<Record<string, unknown>>;
type SourceAlias = { readonly id: string; readonly label: string };

export function projectArtifactForBrowser(value: unknown): UnknownRecord {
  try {
    const projected = projectMarketBrief(value);
    if (JSON.stringify(projected).includes("h1.")) throw new Error("signed handle reached browser projection");
    return projected;
  } catch {
    return unsupportedArtifact;
  }
}

function projectMarketBrief(value: unknown): UnknownRecord {
  const artifact = record(value);
  if (artifact.schema_version !== "market_brief.v1") throw new Error("unsupported artifact");

  const aliases = new Map<string, SourceAlias>();
  const sources = array(artifact.sources).map((value, index) => {
    const source = record(value);
    const rawRef = string(source.citation_ref);
    if (aliases.has(rawRef)) throw new Error("duplicate citation reference");
    const alias = { id: `source-${index + 1}`, label: `Source ${index + 1}` };
    aliases.set(rawRef, alias);
    return {
      citation_ref: alias.id,
      source_alias: alias.label,
      source: string(source.source),
      public_url: nullableString(source.public_url),
      published_at: nullableString(source.published_at),
    };
  });

  return {
    schema_version: "market_brief.v1",
    title: string(artifact.title),
    status: member(artifact.status, ["complete", "partial", "unavailable"]),
    facts: array(artifact.facts).map((fact) => projectFact(fact, aliases)),
    inferences: array(artifact.inferences).map(projectInference),
    limitations: strings(artifact.limitations),
    as_of: nullableString(artifact.as_of),
    sources,
    lineage: projectLineage(artifact.lineage, aliases),
    freshness_warnings: strings(artifact.freshness_warnings),
    published_at: nullableString(artifact.published_at),
    publication_date_warning: boolean(artifact.publication_date_warning),
    datasource_confidence: confidenceMap(artifact.datasource_confidence),
    fact_confidence: confidenceMap(artifact.fact_confidence),
    inference_confidence: confidenceMap(artifact.inference_confidence),
    display_text: string(artifact.display_text),
  };
}

function projectFact(value: unknown, aliases: ReadonlyMap<string, SourceAlias>): UnknownRecord {
  const fact = record(value);
  const common = {
    claim_id: string(fact.claim_id),
    confidence: member(fact.confidence, ["high", "medium", "low"]),
  };
  if (fact.kind === "numeric") {
    return {
      ...common,
      kind: "numeric",
      numeric_value: string(fact.numeric_value),
      numeric_unit: string(fact.numeric_unit),
      numeric_definition: string(fact.numeric_definition),
      numeric_as_of: string(fact.numeric_as_of),
      numeric_source_date: string(fact.numeric_source_date),
      numeric_period_label: string(fact.numeric_period_label),
    };
  }
  if (fact.kind === "qualitative") {
    return {
      ...common,
      kind: "qualitative",
      text: string(fact.text),
      supporting_citation_refs: citationAliases(fact.supporting_citation_refs, aliases),
    };
  }
  throw new Error("unsupported fact");
}

function projectInference(value: unknown): UnknownRecord {
  const inference = record(value);
  return {
    claim_id: string(inference.claim_id),
    text: string(inference.text),
    confidence: member(inference.confidence, ["high", "medium", "low"]),
    supporting_fact_ids: strings(inference.supporting_fact_ids),
    caveat: string(inference.caveat),
  };
}

function projectLineage(value: unknown, aliases: ReadonlyMap<string, SourceAlias>): UnknownRecord {
  const lineage = record(value);
  return Object.fromEntries(Object.entries(lineage).map(([claimId, rawEntry]) => {
    const entry = record(rawEntry);
    return [claimId, {
      observation_ids: strings(entry.observation_ids),
      citation_refs: citationAliases(entry.citation_refs, aliases),
    }];
  }));
}

function citationAliases(value: unknown, aliases: ReadonlyMap<string, SourceAlias>): readonly string[] {
  return strings(value).map((rawRef) => {
    const alias = aliases.get(rawRef);
    if (alias === undefined) throw new Error("unknown citation reference");
    return alias.id;
  });
}

function confidenceMap(value: unknown): UnknownRecord {
  return Object.fromEntries(Object.entries(record(value)).map(([claimId, confidence]) => [
    claimId,
    member(confidence, ["high", "medium", "low"]),
  ]));
}

function record(value: unknown): UnknownRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("record required");
  return value as UnknownRecord;
}

function array(value: unknown): readonly unknown[] {
  if (!Array.isArray(value)) throw new Error("array required");
  return value;
}

function strings(value: unknown): readonly string[] {
  return array(value).map(string);
}

function string(value: unknown): string {
  if (typeof value !== "string") throw new Error("string required");
  return value;
}

function nullableString(value: unknown): string | null {
  return value === null ? null : string(value);
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("boolean required");
  return value;
}

function member<const T extends string>(value: unknown, allowed: readonly T[]): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) throw new Error("enum member required");
  return value as T;
}
