import type { TurnState } from "./runtime.ts";
import type { AgentEventV1 } from "./sse.ts";
import { SessionRegistry } from "./sessions.ts";

export interface RecoveredTurn {
  readonly turn_id: string;
  readonly state: TurnState;
  readonly events: readonly AgentEventV1[];
  readonly artifact?: unknown;
}

type RecoveryResult =
  | { readonly ok: true; readonly turn: RecoveredTurn }
  | { readonly ok: false; readonly reason: "NOT_FOUND" | "SESSION_GONE" };

type RecordResult = { readonly ok: true } | { readonly ok: false; readonly reason: "RECOVERY_LIMIT" };

export class RecoveryStore {
  readonly #registry: SessionRegistry;
  readonly #maxRecoveryRecords: number;
  readonly #records = new Map<string, Map<string, RecoveredTurn>>();

  constructor(registry: SessionRegistry, options: { readonly now?: () => number; readonly maxRecoveryRecords?: number } = {}) {
    this.#registry = registry;
    this.#maxRecoveryRecords = options.maxRecoveryRecords ?? 32;
  }

  record(sessionId: string, turnId: string, state: TurnState, events: readonly AgentEventV1[], artifact?: unknown): RecordResult {
    let turns = this.#records.get(sessionId);
    if (turns === undefined) {
      turns = new Map();
      this.#records.set(sessionId, turns);
    }
    if (!turns.has(turnId) && turns.size >= this.#maxRecoveryRecords) return { ok: false, reason: "RECOVERY_LIMIT" };
    if (!turns.has(turnId)) {
      const reserved = this.#registry.recordRecovery(sessionId);
      if (!reserved.ok) return reserved;
    }
    const turn: RecoveredTurn = artifact === undefined
      ? { turn_id: turnId, state, events: [...events] }
      : { turn_id: turnId, state, events: [...events], artifact };
    turns.set(turnId, turn);
    return { ok: true };
  }

  recover(sessionId: string, turnId: string): RecoveryResult {
    if (this.#registry.status(sessionId) !== "active") return { ok: false, reason: "SESSION_GONE" };
    const turn = this.#records.get(sessionId)?.get(turnId);
    return turn === undefined ? { ok: false, reason: "NOT_FOUND" } : { ok: true, turn };
  }
}
