import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import test from "node:test";

import { CancelCoordinator, raceCancelVsFinalize } from "../src/cancel.ts";
import { createServer } from "../src/http.ts";
import { TurnContext, defaultTurnLimits, type SessionContext } from "../src/runtime.ts";
import { SseHub, SseProtocolError } from "../src/sse.ts";
import { SessionRegistry } from "../src/sessions.ts";

const context: SessionContext = {
  principal: "operator",
  capability_scope_id: "scope",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["market.read"],
  allowed_refresh_profiles: ["bank-rate"],
};

function setup() {
  const registry = new SessionRegistry({ now: () => 0, generationKey: Buffer.alloc(32, 7) });
  const created = registry.createSession(context);
  assert.equal("error" in created, false);
  if ("error" in created) throw new Error("session creation failed");
  const hub = new SseHub(registry, { now: () => 0 });
  const coordinator = new CancelCoordinator({ registry, hub });
  return { registry, hub, coordinator, sessionId: created.handle.id, bearer: created.bearer };
}

function register(setupResult: ReturnType<typeof setup>, turnId = "turn") {
  assert.deepEqual(setupResult.registry.reserveTurn(setupResult.sessionId), { ok: true });
  const turn = new TurnContext(context, defaultTurnLimits, { now: () => 0 });
  setupResult.coordinator.registerActiveTurn(setupResult.sessionId, turnId, turn);
  return turn;
}

test("(a) cancels a mid-tool turn with one cancelled terminal and no artifact", () => {
  const fixture = setup();
  const turn = register(fixture);

  const result = fixture.coordinator.cancel(fixture.sessionId, "turn");
  const events = fixture.hub.events(fixture.sessionId);

  assert.deepEqual(result, { ok: true, terminal_state: "cancelled" });
  assert.equal(turn.isCancelled(), true);
  assert.deepEqual(events.filter((event) => event.type === "artifact.final"), []);
  assert.deepEqual(events.filter((event) => event.type === "turn.completed").map((event) => event.payload), [{ terminal_state: "cancelled" }]);
});

test("(b) cancelling preserves a queued refresh and releases the session for another turn", () => {
  const fixture = setup();
  const refreshRequests = ["refresh_queued"];
  const turn = register(fixture);
  let childStopped = false;
  turn.cancelEvent.onCancel(() => { childStopped = true; });

  fixture.coordinator.cancel(fixture.sessionId, "turn");

  assert.deepEqual(refreshRequests, ["refresh_queued"]);
  assert.equal(childStopped, true);
  assert.deepEqual(fixture.registry.reserveTurn(fixture.sessionId), { ok: true });
});

test("(c) cancelling an already-terminal turn reports not active", () => {
  const fixture = setup();
  register(fixture);
  fixture.coordinator.cancel(fixture.sessionId, "turn");

  assert.deepEqual(fixture.coordinator.cancel(fixture.sessionId, "turn"), { ok: false, reason: "NOT_ACTIVE" });
});

test("(d) cancelling an unknown turn reports unknown turn", () => {
  const fixture = setup();
  assert.deepEqual(fixture.coordinator.cancel(fixture.sessionId, "missing"), { ok: false, reason: "UNKNOWN_TURN" });
});

test("(e) cancel versus finalization emits exactly one terminal", async () => {
  const fixture = setup();
  register(fixture);

  const outcome = await raceCancelVsFinalize(fixture.coordinator, fixture.sessionId, "turn", async () => {
    try {
      fixture.hub.emit(fixture.sessionId, "turn", "turn.failed", {});
    } catch (error) {
      if (!(error instanceof SseProtocolError)) throw error;
    }
  });

  assert.equal(outcome, "finalized");
  assert.equal(fixture.hub.events(fixture.sessionId).filter((event) => event.type === "turn.completed" || event.type === "turn.failed").length, 1);
  assert.equal(fixture.hub.events(fixture.sessionId).some((event) => event.type === "artifact.final"), false);
});

test("(f) cancel winning the child completion race discards a late artifact", () => {
  const fixture = setup();
  register(fixture);

  fixture.coordinator.cancel(fixture.sessionId, "turn");
  assert.throws(() => fixture.hub.emit(fixture.sessionId, "turn", "artifact.final", {}), SseProtocolError);

  assert.equal(fixture.hub.events(fixture.sessionId).some((event) => event.type === "artifact.final"), false);
});

test("(g) the HTTP cancel route returns 202, then 409, and unknown turns return 404", async (t) => {
  const fixture = setup();
  register(fixture);
  const server = createServer(fixture.registry, { sse: fixture.hub, cancel: fixture.coordinator });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error === undefined ? resolve() : reject(error))));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("expected TCP listener");
  const origin = `http://127.0.0.1:${(address as AddressInfo).port}`;
  const headers = { authorization: `Bearer ${fixture.bearer}` };

  const cancelled = await fetch(`${origin}/v1/sessions/${fixture.sessionId}/turns/turn/cancel`, { method: "POST", headers });
  const repeated = await fetch(`${origin}/v1/sessions/${fixture.sessionId}/turns/turn/cancel`, { method: "POST", headers });
  const unknown = await fetch(`${origin}/v1/sessions/${fixture.sessionId}/turns/missing/cancel`, { method: "POST", headers });

  assert.equal(cancelled.status, 202);
  assert.deepEqual(JSON.parse(await cancelled.text()), { turn_id: "turn", state: "cancelled" });
  assert.equal(repeated.status, 409);
  assert.equal(unknown.status, 404);
});
