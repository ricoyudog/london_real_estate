// allow: SIZE_OK - task contract requires the load-bearing runtime core in this one module.
import { createHash, randomBytes } from "node:crypto";
import { performance } from "node:perf_hooks";

export interface SessionContext {
  readonly principal: string;
  readonly capability_scope_id: string;
  readonly allowed_access_classes: readonly string[];
  readonly allowed_capability_ids: readonly string[];
  readonly allowed_refresh_profiles: readonly string[];
}

export interface TurnLimits {
  readonly facadeCallsPerTurn: number;
  readonly statusPollsPerTurn: number;
  readonly finalizeCallsPerTurn: number;
  readonly itemsPerCall: number;
  readonly cumulativeRecords: number;
  readonly cumulativeCitations: number;
  readonly cumulativeModelToolBytes: number;
  readonly refreshWaitMs: number;
  readonly turnDeadlineMs: number;
  readonly renderedTokens: number;
}

export const defaultTurnLimits = {
  facadeCallsPerTurn: 8,
  statusPollsPerTurn: 3,
  finalizeCallsPerTurn: 2,
  itemsPerCall: 20,
  cumulativeRecords: 40,
  cumulativeCitations: 40,
  cumulativeModelToolBytes: 128 * 1024,
  refreshWaitMs: 15_000,
  turnDeadlineMs: 120_000,
  renderedTokens: 4096,
} as const satisfies TurnLimits;

export const TURN_LIMITS = defaultTurnLimits;

export type LedgerEntry = {
  readonly kind: "query" | "citation";
  readonly anchor_as_of: string;
  readonly observation_ids: readonly string[];
  readonly citation_refs: readonly string[];
  readonly numeric_projection?: unknown;
  readonly freshness?: unknown;
};

export class BudgetExceeded extends Error {
  readonly name = "BudgetExceeded";
  readonly budget: keyof TurnLimits;

  constructor(budget: keyof TurnLimits) {
    super(`turn budget exceeded: ${budget}`);
    this.budget = budget;
  }
}

export class TurnDeadlineExceeded extends Error {
  readonly name = "TurnDeadlineExceeded";

  constructor() {
    super("turn deadline exceeded");
  }
}

export class TurnCancelled extends Error {
  readonly name = "TurnCancelled";

  constructor() {
    super("turn cancelled");
  }
}

export class RefreshArgsChanged extends Error {
  readonly name = "RefreshArgsChanged";
  readonly turnId: string;
  readonly toolCallId: string;

  constructor(turnId: string, toolCallId: string) {
    super(`tool-call retry changed refresh arguments: ${turnId}:${toolCallId}`);
    this.turnId = turnId;
    this.toolCallId = toolCallId;
  }
}

export class StateTransitionError extends Error {
  readonly name = "StateTransitionError";
  readonly eventType: AgentEvent["type"];
  readonly currentState: TurnState;

  constructor(eventType: AgentEvent["type"], currentState: TurnState) {
    super(`invalid ${eventType} transition from ${currentState}`);
    this.eventType = eventType;
    this.currentState = currentState;
  }
}

export class CancelToken {
  #cancelled = false;
  readonly #listeners = new Set<() => void>();

  isCancelled(): boolean {
    return this.#cancelled;
  }

  onCancel(listener: () => void): () => void {
    if (this.#cancelled) {
      listener();
      return () => undefined;
    }
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  cancel(): void {
    if (this.#cancelled) return;
    this.#cancelled = true;
    for (const listener of this.#listeners) listener();
    this.#listeners.clear();
  }
}

type TurnCounters = {
  facadeCalls: number;
  statusPolls: number;
  finalizeCalls: number;
  records: number;
  citations: number;
  modelToolBytes: number;
  renderedTokens: number;
};

type ToolCallArguments = {
  readonly items?: number;
  readonly records?: number;
  readonly citations?: number;
  readonly modelToolBytes?: number;
  readonly renderedTokens?: number;
};

type TurnContextOptions = {
  readonly now?: () => number;
  readonly idFactory?: () => string;
};

export class TurnContext {
  readonly session: SessionContext;
  readonly limits: TurnLimits;
  readonly deadline: number;
  readonly cancelEvent = new CancelToken();
  readonly refreshIds = new Map<string, { readonly fingerprint: string; readonly refresh_request_id: string }>();
  readonly #now: () => number;
  readonly #idFactory: () => string;
  readonly #counters: TurnCounters = {
    facadeCalls: 0,
    statusPolls: 0,
    finalizeCalls: 0,
    records: 0,
    citations: 0,
    modelToolBytes: 0,
    renderedTokens: 0,
  };
  readonly #ledger: LedgerEntry[] = [];
  #active = true;

  constructor(ctx: SessionContext, limits: TurnLimits, options: TurnContextOptions = {}) {
    this.session = ctx;
    this.limits = limits;
    this.#now = options.now ?? performance.now.bind(performance);
    this.#idFactory = options.idFactory ?? (() => randomBytes(18).toString("base64url"));
    this.deadline = this.#now() + limits.turnDeadlineMs;
  }

  beforeToolCall(toolName: string, args: ToolCallArguments): void {
    if (this.cancelEvent.isCancelled()) throw new TurnCancelled();
    if (this.#now() >= this.deadline) throw new TurnDeadlineExceeded();
    const next = {
      facadeCalls: this.#counters.facadeCalls + 1,
      statusPolls: this.#counters.statusPolls,
      finalizeCalls: this.#counters.finalizeCalls,
      records: this.#counters.records + (args.records ?? 0),
      citations: this.#counters.citations + (args.citations ?? 0),
      modelToolBytes: this.#counters.modelToolBytes + (args.modelToolBytes ?? 0),
      renderedTokens: this.#counters.renderedTokens + (args.renderedTokens ?? 0),
    };
    switch (toolName) {
      case "get_refresh_status":
        next.statusPolls += 1;
        break;
      case "finalize_market_brief":
        next.finalizeCalls += 1;
        break;
      default:
        break;
    }
    this.#checkItems(args.items);
    this.#checkCounter(next.facadeCalls, "facadeCallsPerTurn");
    this.#checkCounter(next.statusPolls, "statusPollsPerTurn");
    this.#checkCounter(next.finalizeCalls, "finalizeCallsPerTurn");
    this.#checkCounter(next.records, "cumulativeRecords");
    this.#checkCounter(next.citations, "cumulativeCitations");
    this.#checkCounter(next.modelToolBytes, "cumulativeModelToolBytes");
    this.#checkCounter(next.renderedTokens, "renderedTokens");
    this.#counters.facadeCalls = next.facadeCalls;
    this.#counters.statusPolls = next.statusPolls;
    this.#counters.finalizeCalls = next.finalizeCalls;
    this.#counters.records = next.records;
    this.#counters.citations = next.citations;
    this.#counters.modelToolBytes = next.modelToolBytes;
    this.#counters.renderedTokens = next.renderedTokens;
  }

  chargeAccumulators(args: ToolCallArguments): void {
    if (this.cancelEvent.isCancelled()) throw new TurnCancelled();
    if (this.#now() >= this.deadline) throw new TurnDeadlineExceeded();
    this.#checkItems(args.items);
    const next = {
      records: this.#counters.records + (args.records ?? 0),
      citations: this.#counters.citations + (args.citations ?? 0),
      modelToolBytes: this.#counters.modelToolBytes + (args.modelToolBytes ?? 0),
      renderedTokens: this.#counters.renderedTokens + (args.renderedTokens ?? 0),
    };
    this.#checkCounter(next.records, "cumulativeRecords");
    this.#checkCounter(next.citations, "cumulativeCitations");
    this.#checkCounter(next.modelToolBytes, "cumulativeModelToolBytes");
    this.#checkCounter(next.renderedTokens, "renderedTokens");
    this.#counters.records = next.records;
    this.#counters.citations = next.citations;
    this.#counters.modelToolBytes = next.modelToolBytes;
    this.#counters.renderedTokens = next.renderedTokens;
  }

  registerRefreshRequest(turnId: string, toolCallId: string, args: unknown): string {
    const key = `${turnId}:${toolCallId}`;
    const fingerprint = createHash("sha256").update(canonicalJson(args)).digest("hex");
    const existing = this.refreshIds.get(key);
    if (existing !== undefined) {
      if (existing.fingerprint !== fingerprint) throw new RefreshArgsChanged(turnId, toolCallId);
      return existing.refresh_request_id;
    }
    const refresh_request_id = `refresh_${this.#idFactory()}`;
    this.refreshIds.set(key, { fingerprint, refresh_request_id });
    return refresh_request_id;
  }

  getDeadlineRemainingMs(): number {
    return Math.max(0, this.deadline - this.#now());
  }

  isCancelled(): boolean {
    return this.cancelEvent.isCancelled();
  }

  requestCancel(): void {
    this.cancelEvent.cancel();
  }

  addLedgerEntry(entry: LedgerEntry): void {
    this.#ledger.push(entry);
  }

  getLedger(): readonly LedgerEntry[] {
    return this.#ledger;
  }

  isActive(): boolean {
    return this.#active;
  }

  close(): void {
    this.#active = false;
  }

  #checkCounter(value: number, limit: keyof TurnLimits): void {
    if (value > this.limits[limit]) throw new BudgetExceeded(limit);
  }

  #checkItems(items: number | undefined): void {
    if ((items ?? 0) > this.limits.itemsPerCall) throw new BudgetExceeded("itemsPerCall");
  }
}

export type TurnState = "running" | "awaiting_approval" | "completed" | "cancelled" | "failed";

export type TurnFailureReason =
  | "TURN_DEADLINE_EXCEEDED"
  | "NO_FINAL_ARTIFACT"
  | "NUMERIC_GUARD_REJECTED"
  | "RUNTIME_UNAVAILABLE";

export type AgentEvent =
  | { readonly type: "turn.started" }
  | { readonly type: "tool.started"; readonly tool: string }
  | { readonly type: "tool.completed"; readonly tool: string; readonly ok: boolean }
  | { readonly type: "approval.required" }
  | { readonly type: "approval.resolved"; readonly decision: "approve" | "deny" }
  | { readonly type: "artifact.final" }
  | { readonly type: "turn.completed"; readonly terminal_state: "completed" | "cancelled" }
  | { readonly type: "turn.failed"; readonly reason_code: TurnFailureReason };

export class LifecycleReducer {
  #state: TurnState = "running";
  #hasStarted = false;
  #hasFinalArtifact = false;
  #terminal = false;
  #transitioning = false;
  readonly #events: AgentEvent[] = [];

  transition(event: AgentEvent): void {
    if (this.#transitioning) throw new StateTransitionError(event.type, this.#state);
    this.#transitioning = true;
    try {
      this.#apply(event);
      this.#events.push(event);
    } finally {
      this.#transitioning = false;
    }
  }

  state(): TurnState {
    return this.#state;
  }

  events(): readonly AgentEvent[] {
    return this.#events;
  }

  #apply(event: AgentEvent): void {
    if (this.#terminal) throw new StateTransitionError(event.type, this.#state);
    switch (event.type) {
      case "turn.started":
        if (this.#hasStarted) throw new StateTransitionError(event.type, this.#state);
        this.#hasStarted = true;
        return;
      case "tool.started":
      case "tool.completed":
        if (!this.#hasStarted || this.#state !== "running") {
          throw new StateTransitionError(event.type, this.#state);
        }
        return;
      case "approval.required":
        if (!this.#hasStarted || this.#state !== "running") {
          throw new StateTransitionError(event.type, this.#state);
        }
        this.#state = "awaiting_approval";
        return;
      case "approval.resolved":
        if (this.#state !== "awaiting_approval") throw new StateTransitionError(event.type, this.#state);
        this.#state = "running";
        return;
      case "artifact.final":
        if (!this.#hasStarted || this.#state !== "running" || this.#hasFinalArtifact) {
          throw new StateTransitionError(event.type, this.#state);
        }
        this.#hasFinalArtifact = true;
        return;
      case "turn.completed":
        if (
          !this.#hasStarted ||
          (event.terminal_state === "completed" && !this.#hasFinalArtifact)
        ) {
          throw new StateTransitionError(event.type, this.#state);
        }
        this.#state = event.terminal_state;
        this.#terminal = true;
        return;
      case "turn.failed":
        if (!this.#hasStarted) {
          throw new StateTransitionError(event.type, this.#state);
        }
        this.#state = "failed";
        this.#terminal = true;
        return;
      default:
        return assertNever(event);
    }
  }
}

export interface SessionRuntime {
  readonly session: unknown;
  readonly tools: unknown[];
  readonly launcherSession: unknown;
  readonly runTurn: (userMessage: string) => Promise<unknown>;
  readonly getActiveTurnContext: () => TurnContext | undefined;
}

export function createSessionRuntime(ctx: SessionContext): SessionRuntime {
  let activeTurn: TurnContext | undefined;
  return {
    session: null,
    tools: [],
    launcherSession: null,
    async runTurn(_userMessage: string): Promise<unknown> {
      if (activeTurn?.isActive()) return activeTurn;
      activeTurn = new TurnContext(ctx, defaultTurnLimits);
      return activeTurn;
    },
    getActiveTurnContext: () => activeTurn,
  };
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value).sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(",")}}`;
  }
  throw new TypeError("refresh arguments must be JSON serializable");
}

function assertNever(value: never): never {
  throw new TypeError(`unexpected agent event: ${JSON.stringify(value)}`);
}
