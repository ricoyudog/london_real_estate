import assert from "node:assert/strict";
import * as http from "node:http";
import test from "node:test";

import { ModelTextBuffer } from "../src/finalizer.ts";
import { createServer } from "../src/http.ts";
import { LifecycleReducer } from "../src/runtime.ts";
import { SessionEventRing, SseHub, projectLifecycle, streamModelText } from "../src/sse.ts";
import { SessionRegistry } from "../src/sessions.ts";

const sessionId = "session";
const turnId = "turn";
const event = (sequence: number, payload: Readonly<Record<string, unknown>> = {}) => ({
  schema_version: "agent_event.v1" as const,
  sequence,
  event_id: `${sequence}-${sessionId}`,
  session_id: sessionId,
  turn_id: turnId,
  timestamp: "2026-01-01T00:00:00.000Z",
  type: "session.started" as const,
  payload,
});

function registry(): SessionRegistry {
  return new SessionRegistry({ now: () => 0, generationKey: Buffer.alloc(32, 1) });
}

test("(a) emits all decision event types with monotonic agent_event.v1 fields", () => {
  const hub = new SseHub(registry(), { now: () => 0 });
  const types = ["session.started", "turn.started", "message.delta", "tool.started", "tool.completed", "approval.required", "approval.resolved", "artifact.final", "turn.completed", "turn.failed"] as const;
  for (const [index, type] of types.entries()) hub.emit(sessionId, `${turnId}-${index}`, type, {});
  const events = hub.events(sessionId);
  assert.deepEqual(events.map((item) => item.type), types);
  assert.deepEqual(events.map((item) => item.sequence), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  assert.ok(events.every((item) => item.schema_version === "agent_event.v1" && item.event_id === `${item.sequence}-${sessionId}`));
});

test("(b) rejects foreign lifecycle event types", () => {
  const reducer = new LifecycleReducer();
  const hub = new SseHub(registry());
  assert.throws(() => projectLifecycle(sessionId, turnId, hub, reducer, { type: "foreign" } as never));
});

test("(c) evicts the oldest event at 257 events", () => {
  const ring = new SessionEventRing();
  for (let sequence = 1; sequence <= 257; sequence += 1) ring.append(event(sequence));
  assert.equal(ring.has("1-session"), false);
  assert.equal(ring.replay(null).length, 256);
});

test("(d) evicts events once serialized bytes exceed two MiB", () => {
  const ring = new SessionEventRing();
  ring.append(event(1, { text: "x".repeat(2 * 1024 * 1024) }));
  ring.append(event(2));
  assert.equal(ring.has("1-session"), false);
  assert.deepEqual(ring.replay(null).map((item) => item.sequence), [2]);
});

test("(e) replays ordered events after Last-Event-ID", () => {
  const ring = new SessionEventRing();
  for (let sequence = 1; sequence <= 3; sequence += 1) ring.append(event(sequence));
  assert.deepEqual(ring.replay("1-session").map((item) => item.sequence), [2, 3]);
});

test("(f) reports an evicted cursor", () => {
  const hub = new SseHub(registry());
  for (let sequence = 0; sequence < 257; sequence += 1) hub.emit(sessionId, turnId, "session.started", {});
  const replay = hub.replay(sessionId, "1-session");
  assert.equal(replay.ok, false);
  if (replay.ok) throw new Error("expected evicted replay");
  assert.equal(replay.reason, "EVICTED");
});

test("(g) rejects a pre-restart session at the HTTP route", async (t) => {
  const oldRegistry = new SessionRegistry({ now: () => 0, generationKey: Buffer.alloc(32, 2) });
  const session = oldRegistry.createSession({ principal: "operator", allowed_access_classes: [], allowed_capability_ids: [], allowed_refresh_profiles: [] });
  assert.equal("error" in session, false);
  if ("error" in session) throw new Error("session creation failed");
  const server = createServer(new SessionRegistry({ now: () => 0, generationKey: Buffer.alloc(32, 3) }));
  await listen(server);
  t.after(() => close(server));
  const response = await fetch(`${origin(server)}/v1/sessions/${session.handle.id}/events`, { headers: { authorization: `Bearer ${session.bearer}` } });
  assert.equal(response.status, 410);
});

test("(h) refuses a second terminal event", () => {
  const hub = new SseHub(registry());
  hub.emit(sessionId, turnId, "turn.failed", {});
  assert.throws(() => hub.emit(sessionId, turnId, "turn.completed", { terminal_state: "cancelled" }));
});

test("(i) refuses an artifact after a terminal event", () => {
  const hub = new SseHub(registry());
  hub.emit(sessionId, turnId, "turn.failed", {});
  assert.throws(() => hub.emit(sessionId, turnId, "artifact.final", {}));
});

test("(j) projects cancelled lifecycle completion without an artifact", () => {
  const reducer = new LifecycleReducer();
  const hub = new SseHub(registry());
  reducer.transition({ type: "turn.started" });
  reducer.transition({ type: "turn.completed", terminal_state: "cancelled" });
  for (const item of reducer.events()) projectLifecycle(sessionId, turnId, hub, reducer, item);
  assert.deepEqual(hub.events(sessionId).at(-1)?.payload, { terminal_state: "cancelled" });
});

test("(j.1) projects the safe turn failure reason", () => {
  const reducer = new LifecycleReducer();
  const hub = new SseHub(registry());
  reducer.transition({ type: "turn.started" });
  reducer.transition({ type: "turn.failed", reason_code: "NO_FINAL_ARTIFACT" });
  for (const item of reducer.events()) projectLifecycle(sessionId, turnId, hub, reducer, item);
  assert.deepEqual(hub.events(sessionId).at(-1)?.payload, { reason_code: "NO_FINAL_ARTIFACT" });
});

test("(k) replays before entering the live tail", async (t) => {
  const sessionRegistry = registry();
  const created = sessionRegistry.createSession({ principal: "operator", allowed_access_classes: [], allowed_capability_ids: [], allowed_refresh_profiles: [] });
  assert.equal("error" in created, false);
  if ("error" in created) throw new Error("session creation failed");
  const hub = new SseHub(sessionRegistry, { now: () => 0 });
  hub.emit(created.handle.id, turnId, "session.started", {});
  const server = createServer(sessionRegistry, { sse: hub });
  await listen(server);
  t.after(() => close(server));
  let connected!: () => void;
  const connection = new Promise<void>((resolve) => { connected = resolve; });
  const received = new Promise<string>((resolve, reject) => {
    const request = http.get(`${origin(server)}/v1/sessions/${created.handle.id}/events`, { headers: { authorization: `Bearer ${created.bearer}` } }, (response) => {
      connected();
      let text = "";
      response.setEncoding("utf8");
      response.on("data", (chunk: string) => {
        text += chunk;
        if (text.includes("session.started") && text.includes("turn.started")) {
          request.destroy();
          resolve(text);
        }
      });
    });
    request.on("error", reject);
  });
  await connection;
  hub.emit(created.handle.id, turnId, "turn.started", {});
  const text = await received;
  assert.ok(text.indexOf("session.started") < text.indexOf("turn.started"));
});

test("(l) filters numeric chunks from the SSE stream without failing the turn", () => {
  const hub = new SseHub(registry());
  const buffer = new ModelTextBuffer();
  streamModelText(sessionId, turnId, hub, buffer, "4");
  streamModelText(sessionId, turnId, hub, buffer, ".5");
  streamModelText(sessionId, turnId, hub, buffer, "%");
  const events = hub.events(sessionId);
  assert.equal(events.filter((item) => item.type === "message.delta").length, 0);
  assert.equal(events.filter((item) => item.type === "turn.failed").length, 0);
});

function listen(server: http.Server): Promise<void> {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
}

function close(server: http.Server): Promise<void> {
  return new Promise((resolve, reject) => server.close((error) => error === undefined ? resolve() : reject(error)));
}

function origin(server: http.Server): string {
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("expected TCP listener");
  return `http://127.0.0.1:${address.port}`;
}
