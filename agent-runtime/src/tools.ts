import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { type FacadeLauncher, type ToolResult } from "./facade-launcher.ts";
import { DraftRejected } from "./finalizer.ts";
import {
  BudgetExceeded,
  RefreshArgsChanged,
  type SessionContext,
  TurnCancelled,
  type TurnContext,
  TurnDeadlineExceeded,
} from "./runtime.ts";

export type SessionToolDependencies = {
  readonly ctx: SessionContext;
  readonly launcher: FacadeLauncher;
  readonly finalizeBrief: (draft: unknown, turn: TurnContext) => Promise<unknown>;
  readonly getTurnContext: () => TurnContext | undefined;
  readonly onResult?: (toolName: string, args: unknown, result: ToolResult, turn: TurnContext) => void;
  readonly preToolCall?: (toolName: string, args: unknown, turn: TurnContext) => ToolResult | undefined;
};

type ToolErrorCode = "NO_ACTIVE_TURN" | "BUDGET_EXCEEDED" | "TURN_DEADLINE_EXCEEDED" | "TURN_CANCELLED" | "REFRESH_ARGS_CHANGED" | "FINALIZE_REJECTED";
type HostContext = SessionContext & {
  readonly turn_id: string;
  readonly tool_call_id: string;
  readonly refresh_request_id?: string;
};
type Request = {
  readonly schema_version: "agent_tool_request.v1";
  readonly request_id: string;
  readonly arguments: Readonly<Record<string, unknown>>;
  readonly host_context: HostContext;
};
type FacadeToolConfig = {
  readonly name: "describe_market_data" | "query_market_data" | "get_citation_metadata" | "request_data_refresh" | "get_refresh_status";
  readonly label: string;
  readonly description: string;
  readonly parameters: ToolDefinition["parameters"];
  readonly allowed: readonly string[];
};
const hostOnlyQueryFields = ["capability_id", "datasource_ids", "normalized_filters", "result_count"] as const;

const text = Type.String({ minLength: 1, maxLength: 4096, pattern: "\\S" });
const queryKinds = Type.Union([
  Type.Literal("metrics"),
  Type.Literal("supply"),
  Type.Literal("events"),
  Type.Literal("geographies"),
  Type.Literal("health"),
]);
const filterValue = Type.Union([
  Type.String({ minLength: 1, maxLength: 512 }),
  Type.Array(Type.String({ minLength: 1, maxLength: 512 }), { minItems: 1, maxItems: 50 }),
]);
const filters = Type.Object({
  datasource_id: Type.Optional(filterValue),
  category: Type.Optional(filterValue),
  record_type: Type.Optional(filterValue),
  metric_id: Type.Optional(filterValue),
  geography_code: Type.Optional(filterValue),
  provider: Type.Optional(filterValue),
  observation_id: Type.Optional(filterValue),
  evidence_id: Type.Optional(filterValue),
  source_date_from: Type.Optional(Type.String({ minLength: 1, maxLength: 512 })),
  source_date_to: Type.Optional(Type.String({ minLength: 1, maxLength: 512 })),
}, { additionalProperties: false, maxProperties: 10 });
const describeParameters = Type.Object({}, { additionalProperties: false, maxProperties: 0 });
const queryParameters = Type.Object({
  capability_id: text,
  query_kind: queryKinds,
  filters: Type.Optional(filters),
  as_of: Type.Optional(Type.String({ pattern: "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{1,6})?Z$" })),
  cursor_ref: Type.Optional(text),
  limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
}, { additionalProperties: false });
const citationParameters = Type.Object({
  citation_refs: Type.Array(text, { minItems: 1, maxItems: 20, uniqueItems: true }),
}, { additionalProperties: false });
const refreshParameters = Type.Object({
  capability_id: text,
  datasource_id: text,
  request_profile: text,
  bounded_scope: Type.Object({}, {
    maxProperties: 20,
    additionalProperties: Type.Union([
      Type.String({ minLength: 1, maxLength: 512, pattern: "\\S" }),
      Type.Array(Type.String({ minLength: 1, maxLength: 512, pattern: "\\S" }), { minItems: 1, maxItems: 100 }),
    ]),
  }),
  intent: text,
}, { additionalProperties: false });
const statusParameters = Type.Object({ job_ref: text }, { additionalProperties: false });
const confidence = Type.Union([Type.Literal("high"), Type.Literal("medium"), Type.Literal("low")]);
const fact = Type.Object({
  claim_id: text,
  kind: Type.Union([Type.Literal("numeric"), Type.Literal("qualitative")]),
  confidence,
  text: Type.Optional(Type.String({ maxLength: 4096 })),
  supporting_citation_refs: Type.Optional(Type.Array(text, { maxItems: 20, uniqueItems: true })),
  numeric_citation_ref: Type.Optional(text),
}, { additionalProperties: false });
const finalizeParameters = Type.Object({
  title: Type.String({ minLength: 1, maxLength: 4096 }),
  status: Type.Union([Type.Literal("complete"), Type.Literal("partial"), Type.Literal("unavailable")]),
  facts: Type.Array(fact, { maxItems: 12 }),
  inferences: Type.Array(Type.Object({
    claim_id: text,
    text: Type.String({ minLength: 1, maxLength: 4096 }),
    confidence,
    supporting_fact_ids: Type.Array(text, { maxItems: 12, uniqueItems: true }),
    caveat: Type.String({ minLength: 1, maxLength: 4096 }),
  }, { additionalProperties: false }), { maxItems: 8 }),
  limitations: Type.Array(Type.String({ minLength: 1, maxLength: 4096 }), { maxItems: 64 }),
}, { additionalProperties: false });

export function modelVisibleBytes(result: ToolResult): number {
  return Buffer.byteLength(modelVisibleText(result));
}

export function createSessionTools(deps: SessionToolDependencies): ToolDefinition[] {
  const turnIds = new WeakMap<TurnContext, string>();
  const aliases = new ModelHandleAliases();
  let nextTurnId = 0;
  const turnIdFor = (turn: TurnContext): string => {
    const existing = turnIds.get(turn);
    if (existing !== undefined) return existing;
    nextTurnId += 1;
    const turnId = `turn_${nextTurnId}`;
    turnIds.set(turn, turnId);
    return turnId;
  };
  const facadeTool = (config: FacadeToolConfig): ToolDefinition => defineTool({
    name: config.name,
    label: config.label,
    description: config.description,
    parameters: config.parameters,
    executionMode: "sequential",
    async execute(toolCallId, args) {
        const turn = deps.getTurnContext();
        if (turn === undefined) return toolFailure("NO_ACTIVE_TURN");
        try {
          const resolvedArgs = resolveFacadeAliases(config.name, args, aliases);
          const blocked = deps.preToolCall?.(config.name, resolvedArgs, turn);
          if (blocked !== undefined) return toolResult(blocked, aliases);
          const items = argumentItems(resolvedArgs);
        turn.beforeToolCall(config.name, items === undefined ? {} : { items });
        const turn_id = turnIdFor(turn);
        const argumentsValue = stripArguments(resolvedArgs, config.allowed);
        const refresh_request_id = config.name === "request_data_refresh"
          ? turn.registerRefreshRequest(turn_id, toolCallId, argumentsValue)
          : undefined;
        const request: Request = {
          schema_version: "agent_tool_request.v1",
          request_id: `call_${toolCallId}`,
          arguments: argumentsValue,
          host_context: {
            ...deps.ctx,
            turn_id,
            tool_call_id: toolCallId,
            ...(refresh_request_id === undefined ? {} : { refresh_request_id }),
          },
          };
          const result = await deps.launcher.invoke(config.name, request);
          deps.onResult?.(config.name, resolvedArgs, result, turn);
          return toolResult(result, aliases);
      } catch (error) {
        return toolFailure(errorCode(error));
      }
    },
  });

  return [
    facadeTool({ name: "describe_market_data", label: "Describe market data", description: "List the available market-data capabilities.", parameters: describeParameters, allowed: [] }),
    facadeTool({ name: "query_market_data", label: "Query market data", description: "Query bounded canonical market data.", parameters: queryParameters, allowed: ["capability_id", "query_kind", "filters", "as_of", "cursor_ref", "limit"] }),
    facadeTool({ name: "get_citation_metadata", label: "Get citation metadata", description: "Resolve known citation references.", parameters: citationParameters, allowed: ["citation_refs"] }),
    facadeTool({ name: "request_data_refresh", label: "Request data refresh", description: "Request an approved bounded data refresh.", parameters: refreshParameters, allowed: ["capability_id", "datasource_id", "request_profile", "bounded_scope", "intent"] }),
    facadeTool({ name: "get_refresh_status", label: "Get refresh status", description: "Get a refresh job status.", parameters: statusParameters, allowed: ["job_ref"] }),
    defineTool({
      name: "finalize_market_brief",
      label: "Finalize market brief",
      description: "Submit the bounded final market brief draft.",
      parameters: finalizeParameters,
      executionMode: "sequential",
      async execute(_toolCallId, args) {
        const turn = deps.getTurnContext();
        if (turn === undefined) return toolFailure("NO_ACTIVE_TURN");
        try {
          const resolvedArgs = resolveFinalizerAliases(args, aliases);
          const blocked = deps.preToolCall?.("finalize_market_brief", resolvedArgs, turn);
          if (blocked !== undefined) return toolResult(blocked, aliases);
          turn.beforeToolCall("finalize_market_brief", {});
          const result = await deps.finalizeBrief(resolvedArgs, turn);
          const details: ToolResult = { schema_version: "agent_tool_result.v1", request_id: null, status: "ok", data: isRecord(result) ? result : {}, warnings: [], error: null };
          deps.onResult?.("finalize_market_brief", resolvedArgs, details, turn);
          return toolResult(details, aliases);
        } catch (error) {
          if (error instanceof DraftRejected) return finalizeFailure(error);
          return toolFailure(errorCode(error));
        }
      },
    }),
  ];
}

function stripArguments(args: unknown, allowed: readonly string[]): Readonly<Record<string, unknown>> {
  if (!isRecord(args)) return {};
  return Object.fromEntries(Object.entries(args).filter(([key]) => allowed.includes(key)));
}

function argumentItems(args: unknown): number | undefined {
  if (!isRecord(args)) return undefined;
  const citationRefs = args["citation_refs"];
  if (Array.isArray(citationRefs)) return citationRefs.length;
  const limit = args["limit"];
  return typeof limit === "number" ? limit : undefined;
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toolResult(details: ToolResult, aliases?: ModelHandleAliases) {
  return { content: [{ type: "text" as const, text: modelVisibleText(details, aliases) }], details };
}

function modelVisibleText(result: ToolResult, aliases?: ModelHandleAliases): string {
  const data = hideHostQueryMetadata(result.data);
  return JSON.stringify({ status: result.status, data: aliases === undefined ? data : aliases.present(data) });
}

function hideHostQueryMetadata(data: Readonly<Record<string, unknown>> | null): Readonly<Record<string, unknown>> | null {
  if (data === null || !Object.hasOwn(data, "records")) return data;
  return Object.fromEntries(Object.entries(data).filter(([key]) => !hostOnlyQueryFields.some((field) => field === key)));
}

type HandleKind = "citation" | "cursor" | "job";

class ModelHandleAliases {
  readonly #aliasToHandle = new Map<string, string>();
  readonly #handleToAlias = new Map<string, string>();
  readonly #next = new Map<HandleKind, number>();

  present(value: unknown): unknown {
    if (Array.isArray(value)) return value.map((item) => this.present(item));
    if (!isRecord(value)) return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, this.#presentField(key, item)]));
  }

  resolve(kind: HandleKind, value: string): string {
    return this.#aliasToHandle.get(this.#key(kind, value)) ?? value;
  }

  #presentField(key: string, value: unknown): unknown {
    if (key === "citation_ref" && typeof value === "string") return this.#alias("citation", value);
    if (key === "citation_refs" && Array.isArray(value)) return value.map((item) => typeof item === "string" ? this.#alias("citation", item) : this.present(item));
    if (key === "cursor_ref" && typeof value === "string") return this.#alias("cursor", value);
    if (key === "job_ref" && typeof value === "string") return this.#alias("job", value);
    return this.present(value);
  }

  #alias(kind: HandleKind, handle: string): string {
    const handleKey = this.#key(kind, handle);
    const existing = this.#handleToAlias.get(handleKey);
    if (existing !== undefined) return existing;
    const next = (this.#next.get(kind) ?? 0) + 1;
    this.#next.set(kind, next);
    const alias = `${kind}_${next}`;
    this.#handleToAlias.set(handleKey, alias);
    this.#aliasToHandle.set(this.#key(kind, alias), handle);
    return alias;
  }

  #key(kind: HandleKind, value: string): string { return `${kind}\u0000${value}`; }
}

function resolveFacadeAliases(toolName: FacadeToolConfig["name"], args: unknown, aliases: ModelHandleAliases): unknown {
  if (!isRecord(args)) return args;
  switch (toolName) {
    case "query_market_data": return replaceField(args, "cursor_ref", (value) => typeof value === "string" ? aliases.resolve("cursor", value) : value);
    case "get_citation_metadata": return replaceField(args, "citation_refs", (value) => Array.isArray(value) ? value.map((item) => typeof item === "string" ? aliases.resolve("citation", item) : item) : value);
    case "get_refresh_status": return replaceField(args, "job_ref", (value) => typeof value === "string" ? aliases.resolve("job", value) : value);
    default: return args;
  }
}

function resolveFinalizerAliases(value: unknown, aliases: ModelHandleAliases): unknown {
  if (Array.isArray(value)) return value.map((item) => resolveFinalizerAliases(item, aliases));
  if (!isRecord(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => {
    if (key === "numeric_citation_ref" && typeof item === "string") return [key, aliases.resolve("citation", item)];
    if (key === "supporting_citation_refs" && Array.isArray(item)) return [key, item.map((ref) => typeof ref === "string" ? aliases.resolve("citation", ref) : ref)];
    return [key, resolveFinalizerAliases(item, aliases)];
  }));
}

function replaceField(value: Readonly<Record<string, unknown>>, field: string, replace: (item: unknown) => unknown): Readonly<Record<string, unknown>> {
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, key === field ? replace(item) : item]));
}

function finalizeFailure(error: DraftRejected) {
  const message = error.code === "UNRESOLVED_REF"
    ? "For unavailable coverage, submit empty facts and inferences."
    : "Submit a schema-valid brief using only resolved citation aliases.";
  return toolFailure("FINALIZE_REJECTED", message);
}

function toolFailure(code: ToolErrorCode, message = "Tool request unavailable.") {
  const result: ToolResult = {
    schema_version: "agent_tool_result.v1",
    request_id: null,
    status: "error",
    data: null,
    warnings: [],
    error: { code, message, retryable: false },
  };
  return { content: [{ type: "text" as const, text: JSON.stringify({ status: "error", error: { code, message } }) }], details: result };
}

function errorCode(error: unknown): ToolErrorCode {
  if (error instanceof BudgetExceeded) return "BUDGET_EXCEEDED";
  if (error instanceof TurnDeadlineExceeded) return "TURN_DEADLINE_EXCEEDED";
  if (error instanceof TurnCancelled) return "TURN_CANCELLED";
  if (error instanceof RefreshArgsChanged) return "REFRESH_ARGS_CHANGED";
  throw error;
}
