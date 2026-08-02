import { randomBytes } from "node:crypto";
import * as http from "node:http";

import type { ApprovalCoordinator, ApprovalDecision } from "./approval.ts";
import type { CancelCoordinator } from "./cancel.ts";
import type { DashboardOverviewV1 } from "./dashboard.ts";
import type { RecoveryStore } from "./recovery.ts";
import type { SseHub } from "./sse.ts";
import { SessionRegistry } from "./sessions.ts";
import type { StaticAssets } from "./static-assets.ts";

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
  { method: "GET", pattern: new RegExp(`^/v1/sessions/${SESSION_ID}/dashboard/overview$`) },
  { method: "DELETE", pattern: new RegExp(`^/v1/sessions/${SESSION_ID}$`) },
] as const;

type ServerOptions = {
  readonly sse?: SseHub;
  readonly recovery?: RecoveryStore;
  readonly cancel?: CancelCoordinator;
  readonly approval?: ApprovalCoordinator;
  readonly runTurn?: (sessionId: string, turnId: string, userMessage: string) => Promise<void>;
  readonly dashboard?: { readonly overview: (session: { readonly principal: string; readonly scope_id: string }) => Promise<DashboardOverviewV1> };
  readonly staticAssets?: StaticAssets;
};

export function createServer(registry: SessionRegistry, options?: ServerOptions): http.Server {
  const legacyTurns = options === undefined ? new Map<string, "unknown" | "terminal">() : undefined;
  return http.createServer((req, res) => handleRequest(registry, legacyTurns, options, req, res));
}

export function parseSessionId(req: http.IncomingMessage): string | null {
  const path = new URL(req.url ?? "/", "http://localhost").pathname;
  const matched = /^\/v1\/sessions\/([^/]+)/.exec(path);
  if (matched?.[1] === undefined || /%(?![0-9A-Fa-f]{2})/.test(matched[1])) return null;
  return decodeURIComponent(matched[1]);
}

function handleRequest(registry: SessionRegistry, legacyTurns: Map<string, "unknown" | "terminal"> | undefined, options: ServerOptions | undefined, req: http.IncomingMessage, res: http.ServerResponse): void {
  const { sse, recovery, cancel, staticAssets } = options ?? {};
  const path = new URL(req.url ?? "/", "http://localhost").pathname;
  const asset = req.method === "GET" ? staticAssets?.get(path) : undefined;
  if (asset !== undefined) return respondAsset(res, asset);
  const route = ROUTES.find((candidate) => candidate.pattern.test(path));
  if (route === undefined) return respond(res, 404);
  const sessionId = parseSessionId(req);
  if (sessionId !== null && registry.status(sessionId) === "gone") return respond(res, registry.isPreRestartId(sessionId) ? 410 : 404);
  if (req.method !== route.method) return respond(res, 405, { allow: route.method });
  if (route.pattern.source === ROUTES[0].pattern.source) return createSession(registry, res);

  if (sessionId === null) return respond(res, 404);
  const auth = authenticate(registry, sessionId, req, res);
  if (!auth) return;

  if (route.pattern.source === ROUTES[1].pattern.source) return createTurn(registry, legacyTurns, sessionId, req, res, options?.runTurn);
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
  if (route.pattern.source === ROUTES[5].pattern.source) return decideApproval(registry, options?.approval, sessionId, decodeSegment(path, 5), req, res);
  if (route.pattern.source === ROUTES[6].pattern.source) return dashboardOverview(registry, options?.dashboard, sessionId, res);
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

function createTurn(registry: SessionRegistry, legacyTurns: Map<string, "unknown" | "terminal"> | undefined, sessionId: string, req: http.IncomingMessage, res: http.ServerResponse, runTurn?: ServerOptions["runTurn"]): void {
  const reserved = registry.reserveTurn(sessionId);
  if (!reserved.ok) {
    if (reserved.reason === "NO_ACTIVE_TURN") return respondJson(res, 409, { error: "active_turn" });
    if (reserved.reason === "TURN_LIMIT") return respondJson(res, 429, { error: "TURN_LIMIT" });
    return respond(res, registry.isPreRestartId(sessionId) ? 410 : 404);
  }
  const turnId = randomBytes(16).toString("base64url");
  legacyTurns?.set(turnKey(sessionId, turnId), "unknown");
  if (runTurn === undefined) return respondJson(res, 202, { turn_id: turnId });
  void readMessage(req).then((message) => {
    if (message === null) {
      registry.releaseTurn(sessionId);
      return respondJson(res, 400, { error: "invalid_message" });
    }
    respondJson(res, 202, { turn_id: turnId });
    void runTurn(sessionId, turnId, message).catch(() => undefined);
  });
}

async function readMessage(req: http.IncomingMessage): Promise<string | null> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    bytes += buffer.byteLength;
    if (bytes > 65_536) return null;
    chunks.push(buffer);
  }
  try {
    const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    return typeof value === "object" && value !== null && !Array.isArray(value) && typeof (value as Record<string, unknown>)["message"] === "string"
      ? (value as Record<string, string>)["message"] ?? null : null;
  } catch (error) {
    if (error instanceof SyntaxError) return null;
    throw error;
  }
}

function decideApproval(registry: SessionRegistry, approval: ApprovalCoordinator | undefined, sessionId: string, approvalId: string, req: http.IncomingMessage, res: http.ServerResponse): void {
  if (approval === undefined) return respondJson(res, 501, { error: "NOT_IMPLEMENTED" });
  const session = registry.getSession(sessionId);
  if (session === undefined) return respond(res, 410);
  void readDecision(req).then(async (decision) => {
    if (decision === null) return respondJson(res, 400, { error: "invalid_decision" });
    const result = await approval.decide(sessionId, approvalId, decision, session.principal, session.scope_id);
    respondApproval(res, result, decision);
  });
}

async function readDecision(req: http.IncomingMessage): Promise<"approve" | "deny" | null> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    bytes += buffer.byteLength;
    if (bytes > 65_536) return null;
    chunks.push(buffer);
  }
  try {
    const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (typeof value !== "object" || value === null || Array.isArray(value) || Object.keys(value).length !== 1 || !("decision" in value)) return null;
    return value.decision === "approve" || value.decision === "deny" ? value.decision : null;
  } catch (error) {
    if (error instanceof SyntaxError) return null;
    throw error;
  }
}

function respondApproval(res: http.ServerResponse, result: ApprovalDecision, decision: "approve" | "deny"): void {
  if (result.ok) return respondJson(res, 200, { outcome: result.outcome });
  switch (result.reason) {
    case "UNKNOWN":
      return respond(res, 404);
    case "EXPIRED":
      return respondJson(res, 410, { error: "approval_expired" });
    case "REPLAY_OPPOSITE":
      return respondJson(res, 409, { error: "approval_already_resolved", decision });
    case "SCOPE_MISMATCH":
    case "PRINCIPAL_MISMATCH":
    case "FINGERPRINT_MISMATCH":
    case "POLICY_VERSION_MISMATCH":
      return respond(res, 403);
  }
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

function dashboardOverview(registry: SessionRegistry, dashboard: ServerOptions["dashboard"] | undefined, sessionId: string, res: http.ServerResponse): void {
  if (dashboard === undefined) return respond(res, 501);
  const session = registry.getSession(sessionId);
  if (session === undefined) return respond(res, 410);
  void dashboard.overview(session).then(
    (overview) => respondJson(res, 200, overview),
    () => respondJson(res, 503, { error: "dashboard_unavailable" }),
  );
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

function respondAsset(res: http.ServerResponse, asset: { readonly content: string; readonly contentType: string }): void {
  res.writeHead(200, { "content-type": asset.contentType, "cache-control": "no-store", "x-content-type-options": "nosniff" });
  res.end(asset.content);
}
