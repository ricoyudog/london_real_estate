import type { TurnContext } from "./runtime.ts";
import { SseProtocolError, type SseHub } from "./sse.ts";
import type { SessionRegistry } from "./sessions.ts";

type ActiveTurn = {
  readonly turnId: string;
  readonly turnContext: TurnContext;
};

export type CancelResult =
  | { readonly ok: true; readonly terminal_state: "cancelled" }
  | { readonly ok: false; readonly reason: "UNKNOWN_TURN" | "NOT_ACTIVE" | "SESSION_GONE" };

export class CancelCoordinator {
  readonly #registry: SessionRegistry;
  readonly #hub: SseHub | undefined;
  readonly #activeTurns = new Map<string, ActiveTurn>();
  readonly #terminalTurns = new Set<string>();

  constructor(deps: { readonly registry: SessionRegistry; readonly hub?: SseHub }) {
    this.#registry = deps.registry;
    this.#hub = deps.hub;
  }

  registerActiveTurn(sessionId: string, turnId: string, turnContext: TurnContext): void {
    this.#activeTurns.set(sessionId, { turnId, turnContext });
  }

  cancel(sessionId: string, turnId: string): CancelResult {
    if (this.#registry.status(sessionId) !== "active") return { ok: false, reason: "SESSION_GONE" };
    const active = this.#activeTurns.get(sessionId);
    if (active === undefined) return this.#terminalTurns.has(turnKey(sessionId, turnId))
      ? { ok: false, reason: "NOT_ACTIVE" }
      : { ok: false, reason: "UNKNOWN_TURN" };
    if (active.turnId !== turnId) return { ok: false, reason: "UNKNOWN_TURN" };

    active.turnContext.requestCancel();
    if (!this.emitTerminal(sessionId, turnId)) {
      this.#activeTurns.delete(sessionId);
      this.#terminalTurns.add(turnKey(sessionId, turnId));
      this.#registry.releaseTurn(sessionId);
      return { ok: false, reason: "NOT_ACTIVE" };
    }
    this.#activeTurns.delete(sessionId);
    this.#terminalTurns.add(turnKey(sessionId, turnId));
    this.#registry.releaseTurn(sessionId);
    return { ok: true, terminal_state: "cancelled" };
  }

  emitTerminal(sessionId: string, turnId: string): boolean {
    if (this.#hub === undefined) return false;
    try {
      this.#hub.emit(sessionId, turnId, "turn.completed", { terminal_state: "cancelled" });
      return true;
    } catch (error) {
      if (error instanceof SseProtocolError) return false;
      throw error;
    }
  }
}

function turnKey(sessionId: string, turnId: string): string {
  return `${sessionId}:${turnId}`;
}

export async function raceCancelVsFinalize(
  coordinator: CancelCoordinator,
  sessionId: string,
  turnId: string,
  finalize: () => Promise<void>,
): Promise<"cancelled" | "finalized"> {
  const [_, cancelled] = await Promise.all([finalize(), Promise.resolve().then(() => coordinator.cancel(sessionId, turnId))]);
  return cancelled.ok ? "cancelled" : "finalized";
}
