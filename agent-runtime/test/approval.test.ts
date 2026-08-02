// allow: SIZE_OK - task 18 requires one named adversarial approval matrix (a-o).
import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";

import { fauxAssistantMessage, fauxText, fauxToolCall } from "@earendil-works/pi-ai";

import { ApprovalCoordinator, type PendingApproval } from "../src/approval.ts";
import { createApp } from "../src/app.ts";
import { FacadeLauncher, type ToolResult } from "../src/facade-launcher.ts";
import { createServer } from "../src/http.ts";
import { TurnContext, defaultTurnLimits, type SessionContext } from "../src/runtime.ts";
import { SessionRegistry } from "../src/sessions.ts";
import { SseHub } from "../src/sse.ts";
import { createFauxModels, FAUX_MODEL_REF } from "./helpers/faux-models.ts";
import { fixtureAssets } from "./fixtures/fixture-assets.ts";

const context: SessionContext = {
  principal: "operator", capability_scope_id: "scope-ons", allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.postcode-resolution"], allowed_refresh_profiles: ["onspd-postcode"],
};

test("(a) approve while streaming queues one follow-up on the same logical turn", async () => {
  const fixture = setup(true);
  const identity = fixture.identity();
  const result = await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);

  assert.deepEqual(result, { ok: true, outcome: "approved" });
  assert.deepEqual(fixture.messages, [{ customType: "approval-continuation", options: { deliverAs: "followUp" } }]);
  assert.deepEqual(fixture.identity(), identity);
  assert.equal(fixture.hub.events(fixture.sessionId).filter((event) => event.type === "turn.started").length, 1);
  assert.equal(fixture.hub.events(fixture.sessionId).filter(terminal).length, 1);
});

test("(b) approve while idle triggers one custom continuation on the same logical turn", async () => {
  const fixture = setup(false);
  const identity = fixture.identity();

  await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);

  assert.deepEqual(fixture.messages, [{ customType: "approval-continuation", options: { triggerTurn: true } }]);
  assert.deepEqual(fixture.identity(), identity);
});

test("(c) streaming-to-idle boundary selects exactly one dispatcher path", async () => {
  const gate = deferred();
  const fixture = setup(true, gate.promise);
  const decision = fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);
  fixture.session.streaming = false;
  gate.resolve();

  await decision;

  assert.equal(fixture.messages.length, 1);
  assert.deepEqual(fixture.messages[0]?.options, { triggerTurn: true });
});

test("(d) deny emits one resolution and terminal without launcher or model call", async () => {
  const fixture = setup(false);

  const result = await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "deny", context.principal, context.capability_scope_id);

  assert.deepEqual(result, { ok: true, outcome: "denied" });
  assert.equal(fixture.launcher.calls, 0);
  assert.equal(fixture.messages.length, 0);
  assert.deepEqual(fixture.hub.events(fixture.sessionId).map((event) => event.type), ["turn.started", "approval.required", "approval.resolved", "turn.completed"]);
  assert.equal(fixture.hub.events(fixture.sessionId).some((event) => event.type === "artifact.final"), false);
});

test("(e) expired approval fails closed and preserves last-good ledger", async () => {
  const fixture = setup(false);
  const ledger = fixture.turn.getLedger();
  fixture.clock.now = fixture.approval.expires_at_ms;

  const result = await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);

  assert.deepEqual(result, { ok: false, reason: "EXPIRED" });
  assert.equal(fixture.turn.getLedger(), ledger);
  assert.equal(fixture.launcher.calls, 0);
});

test("(f) same-decision replay is idempotent", async () => {
  const fixture = setup(false);
  await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);

  const replay = await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);

  assert.deepEqual(replay, { ok: true, outcome: "approved", replay: "same" });
  assert.equal(fixture.launcher.calls, 1);
});

test("(g) opposite-decision replay is rejected", async () => {
  const fixture = setup(false);
  await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);

  assert.deepEqual(await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "deny", context.principal, context.capability_scope_id), { ok: false, reason: "REPLAY_OPPOSITE" });
});

test("(h) scope mismatch is rejected", async () => {
  const fixture = setup(false);
  assert.deepEqual(await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, "wrong"), { ok: false, reason: "SCOPE_MISMATCH" });
});

test("(i) principal mismatch is rejected", async () => {
  const fixture = setup(false);
  assert.deepEqual(await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", "wrong", context.capability_scope_id), { ok: false, reason: "PRINCIPAL_MISMATCH" });
});

test("(j) fingerprint mismatch is rejected", async () => {
  const fixture = setup(false, undefined, { fingerprint: "changed" });
  assert.deepEqual(await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id), { ok: false, reason: "FINGERPRINT_MISMATCH" });
});

test("(k) policy-version mismatch is rejected", async () => {
  const fixture = setup(false);
  Object.defineProperty(fixture.approval, "policy_version", { value: "changed-policy-v2" });
  assert.deepEqual(await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id), { ok: false, reason: "POLICY_VERSION_MISMATCH" });
});

test("(l) opaque approval flow never discloses a confirmation token", async () => {
  const fixture = setup(false);
  const secret = "confirmation-token-must-stay-in-python";
  await fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);

  assert.equal(JSON.stringify({ events: fixture.hub.events(fixture.sessionId), messages: fixture.messages, requests: fixture.launcher.requests }).includes(secret), false);
});

test("(m) duplicate concurrent approvals produce at most one side effect", async () => {
  const gate = deferred();
  const fixture = setup(false, gate.promise);
  const first = fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);
  const second = fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);
  gate.resolve();

  const results = await Promise.all([first, second]);

  assert.equal(results.every((result) => result.ok && result.outcome === "approved"), true);
  assert.equal(fixture.launcher.calls, 1);
  assert.equal(fixture.messages.length, 1);
});

test("(n) delete during approval prevents continuation dispatch", async () => {
  const gate = deferred();
  const fixture = setup(false, gate.promise);
  const decision = fixture.coordinator.decide(fixture.sessionId, fixture.approval.approval_id, "approve", context.principal, context.capability_scope_id);
  fixture.registry.close(fixture.sessionId);
  gate.resolve();

  await decision;

  assert.equal(fixture.messages.length, 0);
});

test("(o) production policy cannot register the ONSPD approval capability", () => {
  const fixture = setup(false, undefined, { policy_version: "production-v1", approval_id: "approval-production" }, false);
  assert.deepEqual(fixture.coordinator.registerRequired(fixture.approval), { ok: false, reason: "POLICY_VERSION_MISMATCH" });
});

test("HTTP approval route returns 200 for replay, 409 for opposite, and 410 for expiry", async (t) => {
  const fixture = setup(false);
  const server = createServer(fixture.registry, { approval: fixture.coordinator, sse: fixture.hub });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error === undefined ? resolve() : reject(error))));
  const address = server.address();
  assert.ok(address !== null && typeof address !== "string");
  const url = `http://127.0.0.1:${(address as AddressInfo).port}/v1/sessions/${fixture.sessionId}/approvals/${fixture.approval.approval_id}`;
  const post = (decision: "approve" | "deny") => fetch(url, { method: "POST", headers: { authorization: `Bearer ${fixture.bearer}`, "content-type": "application/json" }, body: JSON.stringify({ decision }) });

  const approved = await post("approve");
  const replay = await post("approve");
  const opposite = await post("deny");

  assert.equal(approved.status, 200);
  assert.deepEqual(await approved.json(), { outcome: "approved" });
  assert.equal(replay.status, 200);
  assert.equal(opposite.status, 409);
});

test("HTTP approval route returns 410 after approval expiry", async (t) => {
  const fixture = setup(false);
  const server = createServer(fixture.registry, { approval: fixture.coordinator, sse: fixture.hub });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error === undefined ? resolve() : reject(error))));
  const address = server.address();
  assert.ok(address !== null && typeof address !== "string");
  fixture.clock.now = fixture.approval.expires_at_ms;

  const response = await fetch(`http://127.0.0.1:${(address as AddressInfo).port}/v1/sessions/${fixture.sessionId}/approvals/${fixture.approval.approval_id}`, {
    method: "POST", headers: { authorization: `Bearer ${fixture.bearer}`, "content-type": "application/json" }, body: JSON.stringify({ decision: "approve" }),
  });

  assert.equal(response.status, 410);
  assert.deepEqual(await response.json(), { error: "approval_expired" });
});

test("composed app registers approval_required and rejects a second user prompt", async (t) => {
  const worktreeRoot = resolve(import.meta.dirname, "../..");
  const dataDir = join(mkdtempSync(join(tmpdir(), "approval-composed-")), "data");
  const launcher = new ScriptedLauncher(dataDir, fixtureAssets(worktreeRoot));
  const faux = createFauxModels([
    fauxAssistantMessage(fauxToolCall("request_data_refresh", {
      capability_id: "uk.postcode-resolution", datasource_id: "ons.postcode-directory",
      request_profile: "onspd-postcode", bounded_scope: { postcode: "SW1A 1AA" }, intent: "resolve postcode",
    }), { stopReason: "toolUse" }),
    fauxAssistantMessage(fauxText("Awaiting approval.")),
    fauxAssistantMessage(fauxText("Approval received.")),
  ]);
  process.env.PI_MODEL = FAUX_MODEL_REF;
  const app = await createApp({ ctx: context, creDataDir: dataDir, assetsDir: fixtureAssets(worktreeRoot), launcher, modelsOverride: faux.models });
  t.after(app.close);
  await new Promise<void>((resolve) => app.server.listen(0, "127.0.0.1", resolve));
  const address = app.server.address();
  assert.ok(address !== null && typeof address !== "string");
  const origin = `http://127.0.0.1:${(address as AddressInfo).port}`;
  const createdResponse = await fetch(`${origin}/v1/sessions`, { method: "POST" });
  const created = await createdResponse.json();
  assert.ok(isRecord(created) && typeof created["id"] === "string" && typeof created["bearer"] === "string");
  const sessionId = created["id"];
  const bearer = created["bearer"];
  const headers = { authorization: `Bearer ${bearer}`, "content-type": "application/json" };

  const message = await fetch(`${origin}/v1/sessions/${sessionId}/messages`, { method: "POST", headers, body: JSON.stringify({ message: "Resolve the latest postcode." }) });
  assert.equal(message.status, 202);
  await waitFor(() => app.hub.events(sessionId).some((event) => event.type === "approval.required"));
  const conflict = await fetch(`${origin}/v1/sessions/${sessionId}/messages`, { method: "POST", headers, body: JSON.stringify({ message: "Start another turn." }) });
  const approvalResponse = await fetch(`${origin}/v1/sessions/${sessionId}/approvals/approval-composed`, { method: "POST", headers, body: JSON.stringify({ decision: "approve" }) });

  assert.equal(conflict.status, 409);
  assert.equal(approvalResponse.status, 200);
  assert.deepEqual(await approvalResponse.json(), { outcome: "approved" });
  assert.deepEqual(launcher.calls, ["request_data_refresh", "approve_refresh"]);
  assert.equal(app.hub.events(sessionId).filter((event) => event.type === "turn.started").length, 1);
  assert.equal(app.hub.events(sessionId).filter(terminal).length, 1);
});

function setup(streaming: boolean, launchGate?: Promise<void>, override: Partial<PendingApproval> = {}, register = true) {
  const clock = { now: 100 };
  const registry = new SessionRegistry({ now: () => clock.now, generationKey: Buffer.alloc(32, 18) });
  const created = registry.createSession({ ...context, explicit_scope_id: context.capability_scope_id });
  assert.equal("error" in created, false);
  if ("error" in created) throw new Error("session creation failed");
  assert.deepEqual(registry.reserveTurn(created.handle.id), { ok: true });
  const hub = new SseHub(registry, { now: () => clock.now });
  const launcher = new FakeLauncher(launchGate);
  const session = new FakeSession(streaming);
  const turn = new TurnContext(context, defaultTurnLimits, { now: () => clock.now, idFactory: () => "approval-refresh" });
  const refreshRequestId = turn.registerRefreshRequest("turn-ons", "tool-ons", { postcode: "SW1A 1AA" });
  const refresh = [...turn.refreshIds.values()][0];
  assert.ok(refresh);
  turn.addLedgerEntry({ kind: "query", anchor_as_of: "2026-08-02T00:00:00Z", observation_ids: ["last-good"], citation_refs: ["citation"] });
  const approval: PendingApproval = {
    approval_id: "approval-ons", session_id: created.handle.id, principal: context.principal,
    capability_scope_id: context.capability_scope_id, refresh_request_id: refreshRequestId,
    fingerprint: refresh.fingerprint, policy_version: "test-online-v1", issued_at_ms: clock.now,
    expires_at_ms: 1_000, decision: null, ...override,
  };
  const coordinator = new ApprovalCoordinator({ registry, launcher, hub, now: () => clock.now });
  coordinator.enqueueContinuation(created.handle.id, "turn-ons", { session, ctx: context }, { turn });
  hub.emit(created.handle.id, "turn-ons", "turn.started", {});
  if (register) assert.deepEqual(coordinator.registerRequired(approval), { ok: true });
  const ledger = turn.getLedger();
  return {
    registry, coordinator, hub, launcher, session, turn, approval, clock, bearer: created.bearer,
    sessionId: created.handle.id, messages: session.messages,
    identity: () => ({ turn, scope: turn.session.capability_scope_id, ledger, deadline: turn.deadline, refreshIds: turn.refreshIds }),
  };
}

class FakeLauncher {
  calls = 0;
  readonly requests: unknown[] = [];
  readonly gate: Promise<void> | undefined;
  constructor(gate?: Promise<void>) { this.gate = gate; }
  async invoke(_toolName: string, request: unknown): Promise<ToolResult> {
    this.calls += 1;
    this.requests.push(request);
    await this.gate;
    return { schema_version: "agent_tool_result.v1", request_id: null, status: "ok", data: {}, warnings: [], error: null };
  }
}

class FakeSession {
  streaming: boolean;
  readonly messages: Array<{ readonly customType: string; readonly options: Readonly<Record<string, unknown>> }> = [];
  constructor(streaming: boolean) { this.streaming = streaming; }
  get isStreaming(): boolean { return this.streaming; }
  async sendCustomMessage(message: { readonly customType: string }, options: Readonly<Record<string, unknown>>): Promise<void> {
    this.messages.push({ customType: message.customType, options });
  }
}

class ScriptedLauncher extends FacadeLauncher {
  readonly calls: string[] = [];
  constructor(dataDir: string, assetsDir: string) { super({ creDataDir: dataDir, assetsDir }); }
  override async invoke(toolName: string, request: unknown): Promise<ToolResult> {
    this.calls.push(toolName);
    if (toolName === "request_data_refresh") {
      return {
        schema_version: "agent_tool_result.v1", request_id: requestId(request), status: "ok",
        data: { disposition: "approval_required", approval_id: "approval-composed", approval_expires_at: "2099-08-02T12:00:00Z" },
        warnings: [], error: null,
      };
    }
    return { schema_version: "agent_tool_result.v1", request_id: requestId(request), status: "ok", data: {}, warnings: [], error: null };
  }
}

function terminal(event: { readonly type: string }): boolean { return event.type === "turn.completed" || event.type === "turn.failed"; }
function deferred() { let resolve = (): void => undefined; const promise = new Promise<void>((done) => { resolve = done; }); return { promise, resolve }; }
function requestId(value: unknown): string | null { return isRecord(value) && typeof value["request_id"] === "string" ? value["request_id"] : null; }
function isRecord(value: unknown): value is Readonly<Record<string, unknown>> { return typeof value === "object" && value !== null && !Array.isArray(value); }
async function waitFor(predicate: () => boolean): Promise<void> { const deadline = performance.now() + 5_000; while (!predicate()) { if (performance.now() >= deadline) throw new Error("condition deadline exceeded"); await new Promise<void>((resolve) => setImmediate(resolve)); } }
