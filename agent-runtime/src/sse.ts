// allow: SIZE_OK - task 13's bounded event transport is a single protocol boundary.
import type * as http from "node:http";

import { ModelTextBuffer, NumericGuardViolation } from "./finalizer.ts";
import type { RecoveryStore } from "./recovery.ts";
import type { AgentEvent, LifecycleReducer, TurnFailureReason } from "./runtime.ts";
import { SessionRegistry } from "./sessions.ts";

const EVENT_TYPES = ["session.started", "turn.started", "message.delta", "tool.started", "tool.completed", "approval.required", "approval.resolved", "artifact.final", "turn.completed", "turn.failed"] as const;
const MAX_EVENTS = 256;
const MAX_BYTES = 2 * 1024 * 1024;
const textStreams = new WeakMap<ModelTextBuffer, { held: boolean }>();

export type SseEventType = (typeof EVENT_TYPES)[number];
export type SafePayload = Readonly<Record<string, unknown>>;
export type AgentEventV1 = {
  readonly schema_version: "agent_event.v1";
  readonly sequence: number;
  readonly event_id: string;
  readonly session_id: string;
  readonly turn_id: string;
  readonly timestamp: string;
  readonly type: SseEventType;
  readonly payload: SafePayload;
};

type StoredEvent = { readonly event: AgentEventV1; readonly bytes: number };
type Replay = { readonly ok: true; readonly events: readonly AgentEventV1[] } | { readonly ok: false; readonly reason: "EVICTED" };
type Attachment = { readonly response: http.ServerResponse; readonly queue: string[]; draining: boolean };
type AttachResult = { readonly ok: true } | { readonly ok: false; readonly reason: "UNAUTHENTICATED" | "GONE" | "EVICTED" };

export class SessionEventRing {
  readonly #events: StoredEvent[] = [];
  #bytes = 0;

  append(event: AgentEventV1): { readonly sequence: number; readonly evicted: boolean } {
    const bytes = Buffer.byteLength(JSON.stringify(event));
    this.#events.push({ event, bytes });
    this.#bytes += bytes;
    let evicted = false;
    while (this.#events.length > MAX_EVENTS || this.#bytes > MAX_BYTES) {
      const oldest = this.#events.shift();
      if (oldest === undefined) break;
      this.#bytes -= oldest.bytes;
      evicted = true;
    }
    return { sequence: event.sequence, evicted };
  }

  replay(afterEventId: string | null): AgentEventV1[] {
    if (afterEventId === null) return this.#events.map(({ event }) => event);
    const index = this.#events.findIndex(({ event }) => event.event_id === afterEventId);
    return index < 0 ? [] : this.#events.slice(index + 1).map(({ event }) => event);
  }

  has(eventId: string): boolean {
    return this.#events.some(({ event }) => event.event_id === eventId);
  }
}

export class SseHub {
  readonly #registry: SessionRegistry;
  readonly #now: () => number;
  readonly #rings = new Map<string, SessionEventRing>();
  readonly #sequences = new Map<string, number>();
  readonly #attachments = new Map<string, Set<Attachment>>();
  readonly #terminals = new Set<string>();
  readonly #recovery: RecoveryStore | undefined;

  constructor(registry: SessionRegistry, options: { readonly now?: () => number; readonly recovery?: RecoveryStore } = {}) {
    this.#registry = registry;
    this.#now = options.now ?? (() => performance.timeOrigin + performance.now());
    this.#recovery = options.recovery;
  }

  attach(sessionId: string, res: http.ServerResponse, lastEventId: string | null = null): AttachResult {
    if (this.#registry.status(sessionId) !== "active") return { ok: false, reason: "GONE" };
    const replay = this.replay(sessionId, lastEventId);
    if (!replay.ok) return replay;
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive", "x-accel-buffering": "no" });
    const attachment: Attachment = { response: res, queue: [], draining: false };
    let attached = this.#attachments.get(sessionId);
    if (attached === undefined) {
      attached = new Set();
      this.#attachments.set(sessionId, attached);
    }
    attached.add(attachment);
    this.#write(attachment, "retry: 3000\n\n");
    for (const event of replay.events) this.#write(attachment, frame(event));
    res.once("close", () => this.detach(sessionId, res));
    return { ok: true };
  }

  detach(sessionId: string, res: http.ServerResponse): void {
    const attachments = this.#attachments.get(sessionId);
    if (attachments === undefined) return;
    for (const attachment of attachments) if (attachment.response === res) attachments.delete(attachment);
    if (attachments.size === 0) this.#attachments.delete(sessionId);
  }

  emit(sessionId: string, turnId: string, type: SseEventType, payload: SafePayload = {}): void {
    this.#assertAllowed(type);
    const terminalKey = `${sessionId}:${turnId}`;
    if (this.#terminals.has(terminalKey)) throw new SseProtocolError("turn is already terminal");
    const sequence = (this.#sequences.get(sessionId) ?? 0) + 1;
    this.#sequences.set(sessionId, sequence);
    const event: AgentEventV1 = {
      schema_version: "agent_event.v1", sequence, event_id: `${sequence}-${sessionId}`, session_id: sessionId,
      turn_id: turnId, timestamp: new Date(this.#now()).toISOString(), type, payload,
    };
    this.#ring(sessionId).append(event);
    if (type === "turn.completed" || type === "turn.failed") {
      this.#terminals.add(terminalKey);
      const terminalState = type === "turn.completed" && event.payload.terminal_state === "cancelled"
        ? "cancelled"
        : type === "turn.completed" ? "completed" : "failed";
      const events = this.events(sessionId).filter((item) => item.turn_id === turnId);
      const artifact = events.findLast((item) => item.type === "artifact.final")?.payload;
      const recorded = this.#recovery?.record(sessionId, turnId, terminalState, events, artifact);
      if (recorded !== undefined && !recorded.ok) console.warn("recovery record limit reached", { sessionId, turnId });
    }
    for (const attachment of this.#attachments.get(sessionId) ?? []) this.#write(attachment, frame(event));
  }

  failTurn(sessionId: string, turnId: string, reason_code: TurnFailureReason): void {
    this.emit(sessionId, turnId, "turn.failed", { reason_code });
  }

  close(sessionId: string): void {
    for (const attachment of this.#attachments.get(sessionId) ?? []) attachment.response.end();
    this.#attachments.delete(sessionId);
  }

  replay(sessionId: string, lastEventId: string | null): Replay {
    const ring = this.#rings.get(sessionId);
    if (lastEventId !== null && (ring === undefined || !ring.has(lastEventId))) return { ok: false, reason: "EVICTED" };
    return { ok: true, events: ring?.replay(lastEventId) ?? [] };
  }

  events(sessionId: string): readonly AgentEventV1[] {
    return this.#rings.get(sessionId)?.replay(null) ?? [];
  }

  #ring(sessionId: string): SessionEventRing {
    let ring = this.#rings.get(sessionId);
    if (ring === undefined) {
      ring = new SessionEventRing();
      this.#rings.set(sessionId, ring);
    }
    return ring;
  }

  #write(attachment: Attachment, text: string): void {
    if (attachment.draining) {
      attachment.queue.push(text);
      return;
    }
    if (!attachment.response.write(text)) {
      attachment.draining = true;
      attachment.response.once("drain", () => {
        attachment.draining = false;
        const queued = attachment.queue.splice(0);
        for (const item of queued) this.#write(attachment, item);
      });
    }
  }

  #assertAllowed(type: string): asserts type is SseEventType {
    if (!EVENT_TYPES.includes(type as SseEventType)) throw new SseProtocolError("unknown SSE event type");
  }
}

export function streamModelText(sessionId: string, turnId: string, hub: SseHub, buffer: ModelTextBuffer, chunk: string): void {
  let stream = textStreams.get(buffer);
  if (stream === undefined) {
    stream = { held: false };
    textStreams.set(buffer, stream);
  }
  try {
    buffer.append(chunk);
  } catch (error) {
    if (error instanceof NumericGuardViolation) {
      hub.failTurn(sessionId, turnId, "NUMERIC_GUARD_REJECTED");
      return;
    }
    throw error;
  }
  stream.held ||= /[.\p{N}%％٪\p{Sc}]/u.test(chunk);
  if (!stream.held) hub.emit(sessionId, turnId, "message.delta", { text: chunk });
}

export function projectLifecycle(sessionId: string, turnId: string, hub: SseHub, _reducer: LifecycleReducer, event: AgentEvent, artifact?: unknown): void {
  switch (event.type) {
    case "turn.started":
      return hub.emit(sessionId, turnId, "turn.started", {});
    case "tool.started":
      return hub.emit(sessionId, turnId, "tool.started", { tool: event.tool });
    case "tool.completed":
      return hub.emit(sessionId, turnId, "tool.completed", { tool: event.tool, ok: event.ok });
    case "approval.required":
      return hub.emit(sessionId, turnId, "approval.required", {});
    case "approval.resolved":
      return hub.emit(sessionId, turnId, "approval.resolved", { decision: event.decision });
    case "artifact.final":
      return hub.emit(sessionId, turnId, "artifact.final", { artifact: artifact ?? null });
    case "turn.completed":
      return hub.emit(sessionId, turnId, "turn.completed", { terminal_state: event.terminal_state });
    case "turn.failed":
      return hub.emit(sessionId, turnId, "turn.failed", { reason_code: event.reason_code });
    default:
      return assertNever(event);
  }
}

export class SseProtocolError extends Error {
  readonly name = "SseProtocolError";
  constructor(message: string) { super(message); }
}

function frame(event: AgentEventV1): string {
  return `id: ${event.event_id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

function assertNever(value: never): never {
  throw new SseProtocolError(`unexpected lifecycle event: ${JSON.stringify(value)}`);
}
