import { randomBytes } from "node:crypto";

import type { FacadeLauncher, ToolResult } from "./facade-launcher.ts";
import type { SessionContext } from "./runtime.ts";

const dashboardCapabilityIds = [
  "uk.bank-rate-current",
  "london-planning-activity",
  "london-prime-rent",
  "london-office-vacancy",
  "london-project-supply",
  "uk-investment-transactions",
  "uk-ranked-market-news",
] as const;

type DashboardSession = { readonly principal: string; readonly scope_id: string };
type DashboardLauncher = Pick<FacadeLauncher, "invoke">;
type JsonRecord = Readonly<Record<string, unknown>>;

export type DashboardOverviewV1 = {
  readonly schema_version: "dashboard_overview.v1";
  readonly deployment: {
    readonly mode: "demo" | "production";
    readonly fixture_label: string | null;
  };
  readonly bank_rate: {
    readonly status: "available" | "unavailable";
    readonly value: string | null;
    readonly unit: string | null;
    readonly definition: string | null;
    readonly as_of: string | null;
    readonly source_date: string | null;
    readonly period_label: string | null;
    readonly freshness: {
      readonly retrieval: string;
      readonly observation: string;
      readonly degraded: boolean | null;
    };
    readonly source: {
      readonly publisher: string | null;
      readonly title: string | null;
      readonly public_url: string | null;
      readonly published_at: string | null;
    } | null;
    readonly reason: string | null;
  };
  readonly coverage: readonly {
    readonly capability_id: string;
    readonly status: "supported" | "partial" | "blocked" | "unavailable";
    readonly reason: string | null;
    readonly retrieval_freshness: string;
    readonly observation_freshness: string;
    readonly degraded: boolean | null;
  }[];
};

export class DashboardService {
  readonly #ctx: SessionContext;
  readonly #launcher: DashboardLauncher;
  readonly #deployment: DashboardOverviewV1["deployment"];

  constructor(options: { readonly ctx: SessionContext; readonly launcher: DashboardLauncher; readonly deployment?: DashboardOverviewV1["deployment"] }) {
    this.#ctx = options.ctx;
    this.#launcher = options.launcher;
    this.#deployment = options.deployment ?? { mode: "production", fixture_label: null };
  }

  async overview(session: DashboardSession): Promise<DashboardOverviewV1> {
    const queryContext: SessionContext = {
      ...this.#ctx,
      principal: session.principal,
      capability_scope_id: session.scope_id,
    };
    const describeContext: SessionContext = {
      ...queryContext,
      allowed_capability_ids: dashboardCapabilityIds,
      allowed_refresh_profiles: [],
    };
    const [description, query] = await Promise.all([
      this.#invoke("describe_market_data", {}, describeContext),
      this.#invoke("query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 }, queryContext),
    ]);
    const source = await this.#sourceFor(query, queryContext);
    return {
      schema_version: "dashboard_overview.v1",
      deployment: this.#deployment,
      bank_rate: projectBankRate(query, source),
      coverage: projectCoverage(description),
    };
  }

  async #sourceFor(query: ToolResult, ctx: SessionContext): Promise<JsonRecord | null> {
    const record = firstRecord(query.data);
    const citationRef = text(array(record?.["citation_refs"])[0]);
    if (citationRef === null) return null;
    const result = await this.#invoke("get_citation_metadata", { citation_refs: [citationRef] }, ctx);
    return firstRecord(result.data, "citations");
  }

  #invoke(toolName: string, argumentsValue: JsonRecord, ctx: SessionContext): Promise<ToolResult> {
    const requestId = `dashboard_${randomBytes(16).toString("base64url")}`;
    return this.#launcher.invoke(toolName, {
      schema_version: "agent_tool_request.v1",
      request_id: requestId,
      arguments: argumentsValue,
      host_context: {
        ...ctx,
        turn_id: "dashboard_overview",
        tool_call_id: requestId,
      },
    });
  }
}

function projectBankRate(query: ToolResult, source: JsonRecord | null): DashboardOverviewV1["bank_rate"] {
  const record = firstRecord(query.data);
  const numeric = object(record?.["numeric"]);
  if (query.status === "error" || record === null || numeric === null) {
    return {
      status: "unavailable", value: null, unit: null, definition: null, as_of: null, source_date: null, period_label: null,
      freshness: { retrieval: "unknown", observation: "unknown", degraded: null }, source: null,
      reason: query.error?.code ?? "No canonical Bank Rate record is available.",
    };
  }
  return {
    status: "available",
    value: text(numeric["value"]),
    unit: text(numeric["unit"]),
    definition: text(numeric["definition"]),
    as_of: text(numeric["as_of"]),
    source_date: text(numeric["source_date"]),
    period_label: text(numeric["period_label"]),
    freshness: {
      retrieval: text(record["retrieval_freshness"]) ?? "unknown",
      observation: text(record["observation_freshness"]) ?? "unknown",
      degraded: booleanOrNull(record["degraded"]),
    },
    source: source === null ? null : {
      publisher: text(source["publisher"]), title: text(source["title"]), public_url: text(source["public_url"]), published_at: text(source["published_at"]),
    },
    reason: null,
  };
}

function projectCoverage(description: ToolResult): DashboardOverviewV1["coverage"] {
  const capabilities = array(description.data?.["capabilities"])
    .map(object)
    .filter((value): value is JsonRecord => value !== null);
  return dashboardCapabilityIds.map((capabilityId) => {
    const capability = capabilities.find((value) => value["capability_id"] === capabilityId);
    const availability = object(capability?.["canonical_availability"]);
    const status = text(capability?.["status"]);
    return {
      capability_id: capabilityId,
      status: status === "supported" || status === "partial" || status === "blocked" ? status : "unavailable",
      reason: text(capability?.["blocked_reason"]),
      retrieval_freshness: text(availability?.["retrieval_freshness"]) ?? "unknown",
      observation_freshness: text(availability?.["observation_freshness"]) ?? "unknown",
      degraded: booleanOrNull(availability?.["degraded"]),
    };
  });
}

function firstRecord(value: unknown, key = "records"): JsonRecord | null {
  return object(array(object(value)?.[key])[0]);
}

function array(value: unknown): readonly unknown[] { return Array.isArray(value) ? value : []; }
function object(value: unknown): JsonRecord | null { return typeof value === "object" && value !== null && !Array.isArray(value) ? value as JsonRecord : null; }
function text(value: unknown): string | null { return typeof value === "string" ? value : null; }
function booleanOrNull(value: unknown): boolean | null { return typeof value === "boolean" ? value : null; }
