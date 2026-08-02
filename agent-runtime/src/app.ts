import { randomBytes } from "node:crypto";
import type * as http from "node:http";
import process from "node:process";

import { ApprovalCoordinator, type PendingApproval } from "./approval.ts";
import { bootRuntime, type BootOptions } from "./boot.ts";
import { CancelCoordinator } from "./cancel.ts";
import { DashboardService, type DashboardOverviewV1 } from "./dashboard.ts";
import { FacadeLauncher } from "./facade-launcher.ts";
import { finalizeBrief } from "./finalizer.ts";
import { createServer } from "./http.ts";
import { RecoveryStore } from "./recovery.ts";
import type { SessionContext, TurnContext, TurnFailureReason } from "./runtime.ts";
import type { ToolResult } from "./facade-launcher.ts";
import { SessionRegistry } from "./sessions.ts";
import { LifecycleReducer } from "./runtime.ts";
import { projectLifecycle, SseHub, SseProtocolError } from "./sse.ts";
import { loadDashboardAssets, type StaticAssets } from "./static-assets.ts";
import { runTurn, type TurnOutcome } from "./turn-runner.ts";

export interface AppDeps {
  readonly ctx: SessionContext;
  readonly creDataDir: string;
  readonly assetsDir?: string;
  readonly launcher?: FacadeLauncher;
  readonly modelsOverride?: unknown;
  readonly createSession?: BootOptions["createSession"];
  readonly now?: () => number;
  readonly staticAssets?: StaticAssets;
  readonly deployment?: DashboardOverviewV1["deployment"];
  readonly trace?: (entry: TurnTraceV1) => void;
}

export type TurnTraceV1 = {
  readonly schema_version: "pi_turn_trace.v1";
  readonly turn_id: string;
  readonly runtime_engine: "pi-agent-session";
  readonly model: string;
  readonly tool_sequence: readonly string[];
  readonly terminal_state: TurnOutcome["terminal_state"];
  readonly duration_ms: number;
  readonly reason_code?: TurnFailureReason;
};

export type App = {
  readonly server: http.Server;
  readonly registry: SessionRegistry;
  readonly hub: SseHub;
  readonly recovery: RecoveryStore;
  readonly cancel: CancelCoordinator;
  readonly approval: ApprovalCoordinator;
  readonly runTurnForSession: (sessionId: string, userMessage: string) => Promise<TurnOutcome>;
  readonly close: () => Promise<void>;
};

export async function createApp(deps: AppDeps): Promise<App> {
  const registry = new AppSessionRegistry({ ...(deps.now === undefined ? {} : { now: deps.now }) });
  const recovery = new RecoveryStore(registry);
  const hub = new SseHub(registry, { recovery, ...(deps.now === undefined ? {} : { now: deps.now }) });
  const cancel = new CancelCoordinator({ registry, hub });
  const launcher = deps.launcher ?? new FacadeLauncher({ creDataDir: deps.creDataDir, ...(deps.assetsDir === undefined ? {} : { assetsDir: deps.assetsDir }) });
  const dashboard = new DashboardService({ ctx: deps.ctx, launcher, ...(deps.deployment === undefined ? {} : { deployment: deps.deployment }) });
  const trace = deps.trace ?? ((entry: TurnTraceV1) => { console.log(JSON.stringify(entry)); });
  const approval = new ApprovalCoordinator({ registry, launcher, hub, ...(deps.now === undefined ? {} : { now: deps.now }) });
  const started = new Set<string>();
  const bootedSessions = new Set<unknown>();
  registry.onCreated = (sessionId) => {
    hub.emit(sessionId, "session", "session.started", {});
    started.add(sessionId);
  };

  const execute = async (sessionId: string, turnId: string, userMessage: string): Promise<TurnOutcome> => {
    const startedAt = performance.now();
    let runtimeIdentity = { runtime_engine: "pi-agent-session" as const, model: process.env.PI_MODEL?.trim() || "unavailable" };
    const session = registry.getSession(sessionId);
    if (session === undefined) throw new AppSessionError(sessionId);
    const ctx: SessionContext = { ...deps.ctx, principal: session.principal, capability_scope_id: session.scope_id };
    const previousModel = process.env.PI_MODEL;
    const previousDataDir = process.env.CRE_DATA_DIR;
    process.env.CRE_DATA_DIR = deps.creDataDir;
    try {
      const booted = await bootRuntime(ctx, {
        launcher, finalizeBrief: async (draft, turn) => finalizeBrief({ schema_version: "market_brief_draft.v1", ...record(draft) }, turn),
        ...(deps.modelsOverride === undefined ? {} : { modelsOverride: deps.modelsOverride }),
        ...(deps.createSession === undefined ? {} : { createSession: deps.createSession }),
      });
      runtimeIdentity = booted.runtimeIdentity;
      bootedSessions.add(booted.session);
      const wrapped = approvalRuntime(booted, sessionId, turnId, approval, deps.now ?? (() => performance.timeOrigin + performance.now()));
      const outcome = await runTurn(wrapped, userMessage, {
        populateLedger: true,
        ...(deps.now === undefined ? {} : { now: deps.now }),
        onTurnCreated: (turn) => {
          cancel.registerActiveTurn(sessionId, turnId, turn);
          approval.prepareContinuation(sessionId, turnId, wrapped, turn);
          hub.emit(sessionId, turnId, "turn.started", runtimeIdentity);
        },
      });
      approval.enqueueContinuation(sessionId, turnId, wrapped, outcome);
      const reducer = new LifecycleReducer();
      for (const event of outcome.events.slice(1)) {
        if (outcome.terminal_state === "cancelled") continue;
        if (approval.isAwaiting(sessionId) && (event.type === "turn.completed" || event.type === "turn.failed")) continue;
        projectLifecycle(sessionId, turnId, hub, reducer, event, outcome.artifact);
      }
      trace(turnTrace(turnId, runtimeIdentity, outcome, startedAt));
      return outcome;
    } catch (error) {
      const reason_code = "RUNTIME_UNAVAILABLE" as const;
      try { hub.emit(sessionId, turnId, "turn.failed", { reason_code }); }
      catch (terminalError) { if (!(terminalError instanceof SseProtocolError)) throw terminalError; }
      trace({
        schema_version: "pi_turn_trace.v1",
        turn_id: turnId,
        ...runtimeIdentity,
        tool_sequence: hub.events(sessionId).filter((event) => event.turn_id === turnId && event.type === "tool.started").map((event) => String(event.payload["tool"])),
        terminal_state: "failed",
        duration_ms: elapsedMilliseconds(startedAt),
        reason_code,
      });
      throw error;
    } finally {
      if (!approval.isAwaiting(sessionId)) registry.releaseTurn(sessionId);
      restoreEnv("PI_MODEL", previousModel);
      restoreEnv("CRE_DATA_DIR", previousDataDir);
    }
  };

  const server = createServer(registry, {
    sse: hub, recovery, cancel, approval, dashboard, staticAssets: deps.staticAssets ?? loadDashboardAssets(),
    runTurn: async (sessionId, turnId, message) => { await execute(sessionId, turnId, message); },
  });
  return {
    server, registry, hub, recovery, cancel, approval,
    runTurnForSession: async (sessionId, userMessage) => {
      const reserved = registry.reserveTurn(sessionId);
      if (!reserved.ok) throw new AppSessionError(sessionId);
      return execute(sessionId, randomBytes(16).toString("base64url"), userMessage);
    },
    close: async () => {
      for (const sessionId of started) hub.close(sessionId);
      await Promise.all([...bootedSessions].map(disposeSession));
      if (server.listening) await new Promise<void>((resolve, reject) => server.close((error) => error === undefined ? resolve() : reject(error)));
    },
  };
}

function approvalRuntime(booted: Awaited<ReturnType<typeof bootRuntime>>, sessionId: string, turnId: string, approval: ApprovalCoordinator, now: () => number): Awaited<ReturnType<typeof bootRuntime>> {
  return {
    ...booted,
    setTurnPolicies: (policies) => booted.setTurnPolicies({
      ...(policies.preToolCall === undefined ? {} : { preToolCall: policies.preToolCall }),
      onResult: (toolName, args, result, turn) => {
        policies.onResult?.(toolName, args, result, turn);
        const pending = pendingApproval(toolName, result, turn, sessionId, now());
        if (pending !== undefined) approval.registerRequired(pending);
      },
    }),
  };
}

function turnTrace(turnId: string, identity: { readonly runtime_engine: "pi-agent-session"; readonly model: string }, outcome: TurnOutcome, startedAt: number): TurnTraceV1 {
  return {
    schema_version: "pi_turn_trace.v1",
    turn_id: turnId,
    ...identity,
    tool_sequence: outcome.events.filter((event) => event.type === "tool.started").map((event) => event.tool),
    terminal_state: outcome.terminal_state,
    duration_ms: elapsedMilliseconds(startedAt),
    ...(outcome.reason_code === undefined ? {} : { reason_code: outcome.reason_code }),
  };
}

function elapsedMilliseconds(startedAt: number): number {
  return Math.max(0, Math.round((performance.now() - startedAt) * 1000) / 1000);
}

function pendingApproval(toolName: string, result: ToolResult, turn: TurnContext, sessionId: string, issuedAt: number): PendingApproval | undefined {
  if (toolName !== "request_data_refresh" || result.status !== "ok" || result.data?.["disposition"] !== "approval_required") return undefined;
  if (!turn.session.allowed_capability_ids.includes("uk.postcode-resolution") || !turn.session.allowed_refresh_profiles.includes("onspd-postcode")) return undefined;
  const approvalId = result.data["approval_id"];
  const expiresAt = result.data["approval_expires_at"];
  const refresh = [...turn.refreshIds.values()].at(-1);
  if (typeof approvalId !== "string" || typeof expiresAt !== "string" || refresh === undefined) return undefined;
  const expiresAtMs = Date.parse(expiresAt);
  if (!Number.isFinite(expiresAtMs)) return undefined;
  return {
    approval_id: approvalId,
    session_id: sessionId,
    principal: turn.session.principal,
    capability_scope_id: turn.session.capability_scope_id,
    refresh_request_id: refresh.refresh_request_id,
    fingerprint: refresh.fingerprint,
    policy_version: "test-online-v1",
    issued_at_ms: issuedAt,
    expires_at_ms: expiresAtMs,
    decision: null,
  };
}

class AppSessionError extends Error {
  readonly name = "AppSessionError";
  readonly sessionId: string;
  constructor(sessionId: string) { super(`session is unavailable: ${sessionId}`); this.sessionId = sessionId; }
}

class AppSessionRegistry extends SessionRegistry {
  onCreated: ((sessionId: string) => void) | undefined;
  override createSession(options: Parameters<SessionRegistry["createSession"]>[0]): ReturnType<SessionRegistry["createSession"]> {
    const created = super.createSession(options);
    if (!("error" in created)) this.onCreated?.(created.handle.id);
    return created;
  }
}

function restoreEnv(name: "PI_MODEL" | "CRE_DATA_DIR", value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new TypeError("brief draft must be an object");
  return value as Readonly<Record<string, unknown>>;
}

async function disposeSession(value: unknown): Promise<void> {
  if (typeof value !== "object" || value === null || !("dispose" in value) || typeof value.dispose !== "function") return;
  await value.dispose();
}
