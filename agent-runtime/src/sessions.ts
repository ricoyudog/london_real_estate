import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";

const IDLE_TIMEOUT_MS = 30 * 60 * 1_000;
const DEFAULT_MAX_SESSIONS = 8;
const MAX_TURNS = 16;
const MAX_RECOVERY_RECORDS = 32;
const GENERATION_DOMAIN = "nan-fung/gen/v1";

export type SessionStatus = "active" | "expired" | "gone";

export interface SessionHandle {
  readonly id: string;
  readonly scope_id: string;
  readonly bearer_hash: string;
}

export interface NewSession {
  readonly handle: SessionHandle;
  readonly bearer: string;
  readonly expires_at_ms: number;
}

type CreateSessionOptions = {
  readonly principal: string;
  readonly allowed_access_classes: readonly string[];
  readonly allowed_capability_ids: readonly string[];
  readonly allowed_refresh_profiles: readonly string[];
  readonly explicit_scope_id?: string;
};

type SessionState = {
  readonly principal: string;
  readonly scope_id: string;
  readonly bearer_hash: string;
  expires_at_ms: number;
  active_turn: boolean;
  turn_count: number;
  recovery_count: number;
};

type CreateSessionFailure = { readonly error: "SESSION_LIMIT" };
type Authentication =
  | { readonly ok: true; readonly status: SessionStatus }
  | { readonly ok: false; readonly reason: "UNAUTHENTICATED" | "EXPIRED" | "GONE" };

export class SessionRegistry {
  readonly generation_key: Buffer;
  readonly #now: () => number;
  readonly #maxSessions: number;
  readonly #sessions = new Map<string, SessionState>();
  readonly #expiredSessionIds = new Set<string>();
  readonly #usedScopeIds = new Set<string>();

  constructor(options: { readonly now?: () => number; readonly generationKey?: Buffer; readonly maxSessions?: number } = {}) {
    this.#now = options.now ?? (() => performance.now());
    this.generation_key = Buffer.from(options.generationKey ?? randomBytes(32));
    this.#maxSessions = options.maxSessions ?? DEFAULT_MAX_SESSIONS;
  }

  createSession(options: CreateSessionOptions): NewSession | CreateSessionFailure {
    this.#expireIdleSessions();
    if (this.#sessions.size >= this.#maxSessions) return { error: "SESSION_LIMIT" };
    const scope_id = options.explicit_scope_id ?? `scope_${randomBytes(24).toString("base64url")}`;
    if (this.#usedScopeIds.has(scope_id)) throw new ScopeReusedError(scope_id);
    this.#usedScopeIds.add(scope_id);

    const bearer = randomBytes(32).toString("base64url");
    const bearer_hash = hashBearer(bearer);
    const nonce = randomBytes(16);
    const id = `${generationTag(this.generation_key, nonce).toString("base64url")}.${nonce.toString("base64url")}`;
    const expires_at_ms = this.#now() + IDLE_TIMEOUT_MS;
    this.#sessions.set(id, {
      principal: options.principal,
      scope_id,
      bearer_hash,
      expires_at_ms,
      active_turn: false,
      turn_count: 0,
      recovery_count: 0,
    });
    return { handle: { id, scope_id, bearer_hash }, bearer, expires_at_ms };
  }

  authenticate(id: string, bearer: string): Authentication {
    const state = this.#activeSession(id);
    if (state.status === "gone") return { ok: false, reason: "GONE" };
    if (state.status === "expired") return { ok: false, reason: "EXPIRED" };
    return timingSafeEqual(Buffer.from(state.session.bearer_hash, "hex"), Buffer.from(hashBearer(bearer), "hex"))
      ? { ok: true, status: "active" }
      : { ok: false, reason: "UNAUTHENTICATED" };
  }

  status(id: string): SessionStatus {
    return this.#activeSession(id).status;
  }

  touch(id: string): boolean {
    const state = this.#activeSession(id);
    if (state.status !== "active") return false;
    state.session.expires_at_ms = this.#now() + IDLE_TIMEOUT_MS;
    return true;
  }

  close(id: string): void {
    this.#sessions.delete(id);
  }

  reserveTurn(id: string): { readonly ok: true } | { readonly ok: false; readonly reason: "NO_ACTIVE_TURN" | "TURN_LIMIT" | "NOT_ACTIVE" } {
    const state = this.#activeSession(id);
    if (state.status !== "active") return { ok: false, reason: "NOT_ACTIVE" };
    if (state.session.active_turn) return { ok: false, reason: "NO_ACTIVE_TURN" };
    if (state.session.turn_count >= MAX_TURNS) return { ok: false, reason: "TURN_LIMIT" };
    state.session.active_turn = true;
    state.session.turn_count += 1;
    return { ok: true };
  }

  releaseTurn(id: string): void {
    const state = this.#activeSession(id);
    if (state.status === "active") state.session.active_turn = false;
  }

  recordRecovery(id: string): { readonly ok: true } | { readonly ok: false; readonly reason: "RECOVERY_LIMIT" } {
    const state = this.#activeSession(id);
    if (state.status !== "active" || state.session.recovery_count >= MAX_RECOVERY_RECORDS) {
      return { ok: false, reason: "RECOVERY_LIMIT" };
    }
    state.session.recovery_count += 1;
    return { ok: true };
  }

  getSession(id: string): { readonly principal: string; readonly scope_id: string } | undefined {
    const state = this.#activeSession(id);
    if (state.status !== "active") return undefined;
    return { principal: state.session.principal, scope_id: state.session.scope_id };
  }

  isPreRestartId(id: string): boolean {
    const parts = id.split(".");
    if (parts.length !== 2 || parts[0] === undefined || parts[1] === undefined) return false;
    const tag = decodeBase64url(parts[0]);
    const nonce = decodeBase64url(parts[1]);
    return tag?.byteLength === 16
      && nonce?.byteLength === 16
      && !timingSafeEqual(tag, generationTag(this.generation_key, nonce));
  }

  #activeSession(id: string):
    | { readonly status: "active"; readonly session: SessionState }
    | { readonly status: "expired" }
    | { readonly status: "gone" } {
    if (!this.#isCurrentGenerationId(id)) return { status: "gone" };
    const session = this.#sessions.get(id);
    if (session === undefined) {
      return this.#expiredSessionIds.has(id) ? { status: "expired" } : { status: "gone" };
    }
    if (this.#now() >= session.expires_at_ms) {
      this.#sessions.delete(id);
      this.#expiredSessionIds.add(id);
      return { status: "expired" };
    }
    return { status: "active", session };
  }

  #isCurrentGenerationId(id: string): boolean {
    const parts = id.split(".");
    if (parts.length !== 2 || parts[0] === undefined || parts[1] === undefined) return false;
    const tag = decodeBase64url(parts[0]);
    const nonce = decodeBase64url(parts[1]);
    return tag?.byteLength === 16
      && nonce?.byteLength === 16
      && timingSafeEqual(tag, generationTag(this.generation_key, nonce));
  }

  #expireIdleSessions(): void {
    const now = this.#now();
    for (const [id, session] of this.#sessions) {
      if (now >= session.expires_at_ms) {
        this.#sessions.delete(id);
        this.#expiredSessionIds.add(id);
      }
    }
  }
}

class ScopeReusedError extends Error {
  readonly name = "ScopeReusedError";

  constructor(scopeId: string) {
    super(`capability scope has already been used: ${scopeId}`);
  }
}

function hashBearer(bearer: string): string {
  return createHash("sha256").update(bearer).digest("hex");
}

function generationTag(key: Buffer, nonce: Buffer): Buffer {
  return createHmac("sha256", key).update(GENERATION_DOMAIN).update(nonce).digest().subarray(0, 16);
}

function decodeBase64url(value: string): Buffer | undefined {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) return undefined;
  const decoded = Buffer.from(value, "base64url");
  return decoded.toString("base64url") === value ? decoded : undefined;
}
