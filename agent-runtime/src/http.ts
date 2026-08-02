import { randomBytes } from "node:crypto";
import * as http from "node:http";

import type { CancelCoordinator } from "./cancel.ts";
import type { RecoveryStore } from "./recovery.ts";
import type { SseHub } from "./sse.ts";
import { SessionRegistry } from "./sessions.ts";

const SESSION_ID = "([^/]+)";
const TURN_ID = "([^/]+)";
const APPROVAL_ID = "([^/]+)";
const ROUTES = [
  { method: "POST", pattern: /^\/v1\/sessions$/ },
  { method: "POST", pattern: new RegExp(`^/v1/sessions/${SESSION_ID}/messages$`) },
  { method: "POST", pattern: new RegExp(`^/v1/sessions/${SESSION_ID}/turns/${TURN_ID}/cancel$`) },
  { method: "GET", pattern: new RegExp(`^/v1/sessions/${SESSION_ID}/turns/${TURN_ID}$`) },
  { method: "GET", pattern: new RegExp(`^/v1/sessions/${SESSION_ID}/events$`) },
  { method: "POST", pattern: new RegExp(`^/v1/sessions/${SESSION_ID}/approvals/${APPROVAL_ID}$`) },
  { method: "DELETE", pattern: new RegExp(`^/v1/sessions/${SESSION_ID}$`) },
] as const;

type ServerOptions = { readonly sse?: SseHub; readonly recovery?: RecoveryStore; readonly cancel?: CancelCoordinator };

export function createServer(registry: SessionRegistry, options?: ServerOptions): http.Server {
  const legacyTurns = options === undefined ? new Map<string, "unknown" | "terminal">() : undefined;
  return http.createServer((req, res) => handleRequest(registry, legacyTurns, options?.sse, options?.recovery, options?.cancel, req, res));
}

export function parseSessionId(req: http.IncomingMessage): string | null {
  const path = new URL(req.url ?? "/", "http://localhost").pathname;
  const matched = /^\/v1\/sessions\/([^/]+)/.exec(path);
  if (matched?.[1] === undefined || /%(?![0-9A-Fa-f]{2})/.test(matched[1])) return null;
  return decodeURIComponent(matched[1]);
}

function handleRequest(registry: SessionRegistry, legacyTurns: Map<string, "unknown" | "terminal"> | undefined, sse: SseHub | undefined, recovery: RecoveryStore | undefined, cancel: CancelCoordinator | undefined, req: http.IncomingMessage, res: http.ServerResponse): void {
  const path = new URL(req.url ?? "/", "http://localhost").pathname;
  const route = ROUTES.find((candidate) => candidate.pattern.test(path));
  if (route === undefined) return respond(res, 404);
  const sessionId = parseSessionId(req);
  if (sessionId !== null && registry.status(sessionId) === "gone") return respond(res, registry.isPreRestartId(sessionId) ? 410 : 404);
  if (req.method !== route.method) return respond(res, 405, { allow: route.method });
  if (route.pattern.source === ROUTES[0].pattern.source) return createSession(registry, res);

  if (sessionId === null) return respond(res, 404);
  const auth = authenticate(registry, sessionId, req, res);
  if (!auth) return;

  if (route.pattern.source === ROUTES[1].pattern.source) return createTurn(registry, legacyTurns, sessionId, res);
  if (route.pattern.source === ROUTES[2].pattern.source) return cancelTurn(cancel, legacyTurns, sessionId, decodeSegment(path, 5), res);
  if (route.pattern.source === ROUTES[3].pattern.source) return getTurn(recovery, sessionId, decodeSegment(path, 5), res);
  if (route.pattern.source === ROUTES[4].pattern.source) {
    if (sse === undefined) return respond(res, 200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
    const lastEventId = typeof req.headers["last-event-id"] === "string" ? req.headers["last-event-id"] : null;
    const attached = sse.attach(sessionId, res, lastEventId);
    if (!attached.ok && attached.reason === "EVICTED") return respondJson(res, 410, { error: "replay_evicted" }, { "x-replay-evicted": "true" });
    if (!attached.ok) return respond(res, 410);
    return;
  }
  if (route.pattern.source === ROUTES[5].pattern.source) return respondJson(res, 501, { error: "NOT_IMPLEMENTED" });
  registry.close(sessionId);
  return respond(res, 204);
}

function createSession(registry: SessionRegistry, res: http.ServerResponse): void {
  const created = registry.createSession({
    principal: "anonymous",
    allowed_access_classes: [],
    allowed_capability_ids: [],
    allowed_refresh_profiles: [],
  });
  if ("error" in created) return respondJson(res, 429, { error: created.error });
  respondJson(res, 201, { id: created.handle.id, bearer: created.bearer, scope_id: created.handle.scope_id });
}

function authenticate(registry: SessionRegistry, sessionId: string, req: http.IncomingMessage, res: http.ServerResponse): boolean {
  const status = registry.status(sessionId);
  if (status === "gone") return respond(res, registry.isPreRestartId(sessionId) ? 410 : 404), false;
  const bearer = parseBearer(req.headers.authorization);
  if (bearer === null) return respond(res, 401), false;
  const authenticated = registry.authenticate(sessionId, bearer);
  if (!authenticated.ok) {
    if (authenticated.reason === "EXPIRED") return respond(res, 401, { "x-session-status": "expired" }), false;
    return respond(res, authenticated.reason === "GONE" && registry.isPreRestartId(sessionId) ? 410 : 401), false;
  }
  registry.touch(sessionId);
  return true;
}

function createTurn(registry: SessionRegistry, legacyTurns: Map<string, "unknown" | "terminal"> | undefined, sessionId: string, res: http.ServerResponse): void {
  const reserved = registry.reserveTurn(sessionId);
  if (!reserved.ok) {
    if (reserved.reason === "NO_ACTIVE_TURN") return respondJson(res, 409, { error: "active_turn" });
    if (reserved.reason === "TURN_LIMIT") return respondJson(res, 429, { error: "TURN_LIMIT" });
    return respond(res, registry.isPreRestartId(sessionId) ? 410 : 404);
  }
  const turnId = randomBytes(16).toString("base64url");
  legacyTurns?.set(turnKey(sessionId, turnId), "unknown");
  respondJson(res, 202, { turn_id: turnId });
}

function getTurn(recovery: RecoveryStore | undefined, sessionId: string, turnId: string, res: http.ServerResponse): void {
  if (recovery === undefined) return respond(res, 501);
  const recovered = recovery.recover(sessionId, turnId);
  if (!recovered.ok) return respond(res, recovered.reason === "SESSION_GONE" ? 410 : 404);
  const { turn } = recovered;
  respondJson(res, 200, turn.artifact === undefined
    ? { turn_id: turn.turn_id, state: turn.state, events: turn.events }
    : { turn_id: turn.turn_id, state: turn.state, events: turn.events, artifact: turn.artifact });
}

function cancelTurn(cancel: CancelCoordinator | undefined, legacyTurns: Map<string, "unknown" | "terminal"> | undefined, sessionId: string, turnId: string, res: http.ServerResponse): void {
  if (cancel === undefined) return cancelLegacyTurn(legacyTurns, sessionId, turnId, res);
  const result = cancel.cancel(sessionId, turnId);
  if (result.ok) return respondJson(res, 202, { turn_id: turnId, state: "cancelled" });
  switch (result.reason) {
    case "UNKNOWN_TURN":
      return respond(res, 404);
    case "NOT_ACTIVE":
      return respondJson(res, 409, { error: "already_terminal" });
    case "SESSION_GONE":
      return respond(res, 410);
  }
}

function cancelLegacyTurn(turns: Map<string, "unknown" | "terminal"> | undefined, sessionId: string, turnId: string, res: http.ServerResponse): void {
  if (turns === undefined) return respond(res, 501);
  const key = turnKey(sessionId, turnId);
  const state = turns.get(key);
  if (state === undefined) return respond(res, 404);
  if (state === "terminal") return respondJson(res, 409, { error: "already_terminal" });
  turns.set(key, "terminal");
  return respond(res, 202);
}

function turnKey(sessionId: string, turnId: string): string {
  return `${sessionId}:${turnId}`;
}

function parseBearer(value: string | undefined): string | null {
  const matched = /^Bearer ([A-Za-z0-9_-]+)$/.exec(value ?? "");
  return matched?.[1] === undefined ? null : matched[1];
}

function decodeSegment(path: string, index: number): string {
  const segment = path.split("/")[index];
  return segment === undefined || /%(?![0-9A-Fa-f]{2})/.test(segment) ? "" : decodeURIComponent(segment);
}

function respond(res: http.ServerResponse, status: number, headers: http.OutgoingHttpHeaders = {}): void {
  res.writeHead(status, headers);
  res.end();
}

function respondJson(res: http.ServerResponse, status: number, body: Readonly<Record<string, unknown>>, headers: http.OutgoingHttpHeaders = {}): void {
  res.writeHead(status, { "content-type": "application/json", ...headers });
  res.end(JSON.stringify(body));
}
