// allow: SIZE_OK - approval state, coordination, and dispatch stay together as one lifecycle boundary.
import { randomBytes } from "node:crypto";

import type { FacadeLauncher, ToolResult } from "./facade-launcher.ts";
import type { SessionContext, TurnContext } from "./runtime.ts";
import type { SessionRegistry } from "./sessions.ts";
import type { SseHub } from "./sse.ts";
import { LifecycleReducer } from "./runtime.ts";
import { projectLifecycle } from "./sse.ts";
import { resumeTurn, type TurnOutcome } from "./turn-runner.ts";

const TEST_POLICY_VERSION = "test-online-v1";

export interface PendingApproval {
  readonly approval_id: string;
  readonly session_id: string;
  readonly principal: string;
  readonly capability_scope_id: string;
  readonly refresh_request_id: string;
  readonly fingerprint: string;
  readonly policy_version: string;
  readonly issued_at_ms: number;
  readonly expires_at_ms: number;
  decision: null | "approve" | "deny";
}

type PiSession = {
  readonly isStreaming: boolean;
  readonly sendCustomMessage: (
    message: { readonly customType: string; readonly content: string; readonly display: string },
    options: { readonly deliverAs: "followUp" } | { readonly triggerTurn: true },
  ) => Promise<void>;
  readonly waitForIdle?: () => Promise<void>;
};

type Continuation = {
  readonly turnId: string;
  readonly booted: {
    readonly session: unknown;
    readonly ctx: SessionContext;
    readonly setTurnContext: (turn: TurnContext | undefined) => void;
  };
  readonly turn: TurnContext;
  readonly outcome: Promise<TurnOutcome>;
  readonly resolveOutcome: (outcome: TurnOutcome) => void;
};
type ResumeContinuation = (booted: Continuation["booted"], outcome: TurnOutcome, continueSession?: () => Promise<void>) => Promise<TurnOutcome>;

type DecisionFailure =
  | "UNKNOWN"
  | "EXPIRED"
  | "REPLAY_OPPOSITE"
  | "SCOPE_MISMATCH"
  | "PRINCIPAL_MISMATCH"
  | "FINGERPRINT_MISMATCH"
  | "POLICY_VERSION_MISMATCH";

export type ApprovalDecision =
  | { readonly ok: true; readonly outcome: "approved" | "denied"; readonly replay?: "same" }
  | { readonly ok: false; readonly reason: DecisionFailure };

type ApprovalLauncher = Pick<FacadeLauncher, "invoke">;

export class ApprovalCoordinator {
  readonly #registry: SessionRegistry;
  readonly #launcher: ApprovalLauncher;
  readonly #hub: SseHub | undefined;
  readonly #now: () => number;
  readonly #policyVersion: string;
  readonly #resume: ResumeContinuation;
  readonly #pending = new Map<string, PendingApproval>();
  readonly #continuations = new Map<string, Continuation>();
  readonly #queue = new Map<string, Continuation>();
  #decisionTail: Promise<void> = Promise.resolve();
  #dispatching = false;

  constructor(deps: {
    readonly registry: SessionRegistry;
    readonly launcher: ApprovalLauncher;
    readonly hub?: SseHub;
    readonly now?: () => number;
    readonly policyVersion?: string;
    readonly resume?: ResumeContinuation;
  }) {
    this.#registry = deps.registry;
    this.#launcher = deps.launcher;
    this.#hub = deps.hub;
    this.#now = deps.now ?? (() => performance.timeOrigin + performance.now());
    this.#policyVersion = deps.policyVersion ?? TEST_POLICY_VERSION;
    this.#resume = deps.resume ?? ((booted, outcome, continueSession) => resumeTurn(booted as Parameters<typeof resumeTurn>[0], outcome, continueSession));
  }

  registerRequired(approval: PendingApproval): { readonly ok: true } | { readonly ok: false; readonly reason: "ALREADY_EXISTS" | "POLICY_VERSION_MISMATCH" } {
    if (approval.policy_version !== TEST_POLICY_VERSION || approval.policy_version !== this.#policyVersion) {
      return { ok: false, reason: "POLICY_VERSION_MISMATCH" };
    }
    if (this.#pending.has(approval.approval_id)) return { ok: false, reason: "ALREADY_EXISTS" };
    this.#pending.set(approval.approval_id, approval);
    this.#hub?.emit(approval.session_id, this.#continuations.get(approval.session_id)?.turnId ?? "host-approval", "approval.required", { approval_id: approval.approval_id });
    return { ok: true };
  }

  enqueueContinuation(sessionId: string, turnId: string, booted: Continuation["booted"], outcome: TurnOutcome): void {
    const existing = this.#continuations.get(sessionId);
    if (existing !== undefined && existing.turn === outcome.turn) {
      existing.resolveOutcome(outcome);
      return;
    }
    this.#continuations.set(sessionId, readyContinuation(turnId, booted, outcome.turn, outcome));
  }

  prepareContinuation(sessionId: string, turnId: string, booted: Continuation["booted"], turn: TurnContext): void {
    this.#continuations.set(sessionId, readyContinuation(turnId, booted, turn));
  }

  isAwaiting(sessionId: string): boolean {
    return [...this.#pending.values()].some((approval) => approval.session_id === sessionId && approval.decision === null && this.#now() < approval.expires_at_ms);
  }

  async decide(
    sessionId: string,
    approvalId: string,
    decision: "approve" | "deny",
    principal: string,
    scopeId: string,
  ): Promise<ApprovalDecision> {
    return this.#serialize(() => this.#decide(sessionId, approvalId, decision, principal, scopeId));
  }

  async #decide(sessionId: string, approvalId: string, decision: "approve" | "deny", principal: string, scopeId: string): Promise<ApprovalDecision> {
    const approval = this.#pending.get(approvalId);
    if (approval === undefined || approval.session_id !== sessionId) return { ok: false, reason: "UNKNOWN" };
    if (this.#now() >= approval.expires_at_ms) return { ok: false, reason: "EXPIRED" };
    if (approval.capability_scope_id !== scopeId) return { ok: false, reason: "SCOPE_MISMATCH" };
    if (approval.principal !== principal) return { ok: false, reason: "PRINCIPAL_MISMATCH" };
    if (approval.policy_version !== this.#policyVersion || approval.policy_version !== TEST_POLICY_VERSION) return { ok: false, reason: "POLICY_VERSION_MISMATCH" };
    const continuation = this.#continuations.get(sessionId);
    if (continuation !== undefined && !refreshMatches(continuation.turn, approval)) return { ok: false, reason: "FINGERPRINT_MISMATCH" };
    if (approval.decision !== null) {
      if (approval.decision !== decision) return { ok: false, reason: "REPLAY_OPPOSITE" };
      return { ok: true, outcome: decision === "approve" ? "approved" : "denied", replay: "same" };
    }

    if (decision === "approve") {
      const result = await this.#launcher.invoke("approve_refresh", {
        schema_version: "agent_tool_request.v1",
        request_id: `call_${randomBytes(18).toString("base64url")}`,
        arguments: { approval_id: approvalId, decision: "approve" },
        host_context: {
          principal,
          capability_scope_id: scopeId,
          turn_id: "host-approval",
          tool_call_id: "host-approval",
          allowed_access_classes: continuation?.booted.ctx.allowed_access_classes ?? [],
          allowed_capability_ids: continuation?.booted.ctx.allowed_capability_ids ?? [],
          allowed_refresh_profiles: continuation?.booted.ctx.allowed_refresh_profiles ?? [],
        },
      });
      if (result.status === "error") return { ok: false, reason: "UNKNOWN" };
    }
    approval.decision = decision;

    const turnId = continuation?.turnId ?? "host-approval";
    this.#hub?.emit(sessionId, turnId, "approval.resolved", { decision });
    if (decision === "deny") {
      this.#hub?.emit(sessionId, turnId, "turn.completed", { terminal_state: "completed" });
      this.#registry.releaseTurn(sessionId);
      return { ok: true, outcome: "denied" };
    }
    if (continuation !== undefined && this.#registry.status(sessionId) === "active" && this.#now() < approval.expires_at_ms) {
      this.#queue.set(sessionId, continuation);
      await this.#dispatch();
    }
    return { ok: true, outcome: "approved" };
  }

  async #dispatch(): Promise<void> {
    if (this.#dispatching) return;
    this.#dispatching = true;
    try {
      while (this.#queue.size > 0) {
        const entry = this.#queue.entries().next().value;
        if (entry === undefined) return;
        const [sessionId, continuation] = entry;
        this.#queue.delete(sessionId);
        if (this.#registry.status(sessionId) !== "active") continue;
        if (continuation.turn.getDeadlineRemainingMs() === 0) {
          this.#hub?.emit(sessionId, continuation.turnId, "turn.failed", {});
          this.#registry.releaseTurn(sessionId);
          continue;
        }
        const outcome = await continuation.outcome;
        const session = piSession(continuation.booted.session);
        const message = { customType: "approval-continuation", content: "Refresh approved — continue the same turn.", display: "Refresh approved" };
        continuation.booted.setTurnContext?.(continuation.turn);
        try {
          const previousEventCount = outcome.events.length;
          const resumed = await this.#resume(continuation.booted, outcome, async () => {
            await session.sendCustomMessage(message, session.isStreaming ? { deliverAs: "followUp" } : { triggerTurn: true });
            await session.waitForIdle?.();
          });
          const reducer = new LifecycleReducer();
          for (const event of resumed.events.slice(previousEventCount)) {
            if (this.#hub !== undefined) projectLifecycle(sessionId, continuation.turnId, this.#hub, reducer, event, resumed.artifact);
          }
        } catch {
          this.#hub?.emit(sessionId, continuation.turnId, "turn.failed", {});
        } finally {
          continuation.booted.setTurnContext?.(undefined);
        }
        if (this.#registry.status(sessionId) === "active") {
          this.#registry.releaseTurn(sessionId);
        }
      }
    } finally {
      this.#dispatching = false;
      if (this.#queue.size > 0) void this.#dispatch();
    }
  }

  async #serialize<T>(operation: () => Promise<T>): Promise<T> {
    const previous = this.#decisionTail;
    let release = (): void => undefined;
    this.#decisionTail = new Promise<void>((resolve) => { release = resolve; });
    await previous;
    try {
      return await operation();
    } finally {
      release();
    }
  }
}

function refreshMatches(turn: TurnContext, approval: PendingApproval): boolean {
  return [...turn.refreshIds.values()].some((refresh) => refresh.refresh_request_id === approval.refresh_request_id && refresh.fingerprint === approval.fingerprint);
}

function readyContinuation(turnId: string, booted: Continuation["booted"], turn: TurnContext, ready?: TurnOutcome): Continuation {
  let resolveOutcome = (_outcome: TurnOutcome): void => undefined;
  const outcome = ready === undefined
    ? new Promise<TurnOutcome>((resolve) => { resolveOutcome = resolve; })
    : Promise.resolve(ready);
  return { turnId, booted, turn, outcome, resolveOutcome };
}

function piSession(value: unknown): PiSession {
  if (typeof value !== "object" || value === null || !("isStreaming" in value) || typeof value.isStreaming !== "boolean" || !("sendCustomMessage" in value) || typeof value.sendCustomMessage !== "function") {
    throw new TypeError("booted session lacks approval continuation surface");
  }
  const sendCustomMessage = value.sendCustomMessage;
  const waitForIdle = "waitForIdle" in value && typeof value.waitForIdle === "function" ? value.waitForIdle : undefined;
  return {
    get isStreaming() { return value.isStreaming === true; },
    sendCustomMessage: (message, options) => sendCustomMessage.call(value, message, options),
    ...(waitForIdle === undefined ? {} : { waitForIdle: () => waitForIdle.call(value) }),
  };
}
