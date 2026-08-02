import assert from "node:assert/strict";
import * as http from "node:http";
import test from "node:test";

import { createServer } from "../src/http.ts";
import { RecoveryStore } from "../src/recovery.ts";
import { SseHub } from "../src/sse.ts";
import { SessionRegistry } from "../src/sessions.ts";

const sessionOptions = {
  principal: "operator",
  allowed_access_classes: [],
  allowed_capability_ids: [],
  allowed_refresh_profiles: [],
} as const;

test("(a) recovers a completed turn with ordered events and its final artifact", async (t) => {
  const registry = fixedRegistry(1);
  const session = createdSession(registry);
  const recovery = new RecoveryStore(registry);
  const hub = new SseHub(registry, { now: () => 0, recovery });
  hub.emit(session.id, "complete", "turn.started");
  hub.emit(session.id, "complete", "artifact.final", { report: "final" });
  hub.emit(session.id, "complete", "turn.completed", { terminal_state: "completed" });

  const server = await liveServer(registry, recovery);
  t.after(server.close);
  const response = await fetch(`${server.origin}/v1/sessions/${session.id}/turns/complete`, { headers: bearer(session.bearer) });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    turn_id: "complete",
    state: "completed",
    events: [
      { schema_version: "agent_event.v1", sequence: 1, event_id: `1-${session.id}`, session_id: session.id, turn_id: "complete", timestamp: "1970-01-01T00:00:00.000Z", type: "turn.started", payload: {} },
      { schema_version: "agent_event.v1", sequence: 2, event_id: `2-${session.id}`, session_id: session.id, turn_id: "complete", timestamp: "1970-01-01T00:00:00.000Z", type: "artifact.final", payload: { report: "final" } },
      { schema_version: "agent_event.v1", sequence: 3, event_id: `3-${session.id}`, session_id: session.id, turn_id: "complete", timestamp: "1970-01-01T00:00:00.000Z", type: "turn.completed", payload: { terminal_state: "completed" } },
    ],
    artifact: { report: "final" },
  });
});

test("(b) recovers a recorded mid-flight turn", () => {
  const registry = fixedRegistry(2);
  const session = createdSession(registry);
  const recovery = new RecoveryStore(registry);
  const hub = new SseHub(registry, { now: () => 0 });
  hub.emit(session.id, "running", "turn.started");
  hub.emit(session.id, "running", "message.delta", { text: "partial" });

  assert.deepEqual(recovery.record(session.id, "running", "running", hub.events(session.id)), { ok: true });
  const recovered = recovery.recover(session.id, "running");

  assert.equal(recovered.ok, true);
  if (!recovered.ok) throw new Error("expected recovered turn");
  assert.equal(recovered.turn.state, "running");
  assert.deepEqual(recovered.turn.events.map((event) => event.event_id), [`1-${session.id}`, `2-${session.id}`]);
});

test("(c) terminal recovery survives ring eviction and enforces the per-session limit", () => {
  const registry = fixedRegistry(3);
  const session = createdSession(registry);
  const recovery = new RecoveryStore(registry);
  const hub = new SseHub(registry, { now: () => 0, recovery });
  hub.emit(session.id, "evicted", "turn.started");
  hub.emit(session.id, "evicted", "turn.failed");
  for (let index = 0; index < 257; index += 1) hub.emit(session.id, `other-${index}`, "session.started");

  const recovered = recovery.recover(session.id, "evicted");
  assert.equal(recovered.ok, true);
  if (!recovered.ok) throw new Error("expected recovered terminal turn");
  assert.deepEqual(recovered.turn.events.map((event) => event.event_id), [`1-${session.id}`, `2-${session.id}`]);

  for (let index = 0; index < 31; index += 1) {
    assert.deepEqual(recovery.record(session.id, `limited-${index}`, "running", []), { ok: true });
  }
  assert.deepEqual(recovery.record(session.id, "limited-final", "running", []), { ok: false, reason: "RECOVERY_LIMIT" });
});

test("(d) a replacement registry reports process-restart recovery as gone", async (t) => {
  const original = fixedRegistry(4);
  const session = createdSession(original);
  const replacement = fixedRegistry(5);
  const recovery = new RecoveryStore(replacement);
  const server = await liveServer(replacement, recovery);
  t.after(server.close);

  assert.deepEqual(recovery.recover(session.id, "old-turn"), { ok: false, reason: "SESSION_GONE" });
  const response = await fetch(`${server.origin}/v1/sessions/${session.id}/turns/old-turn`, { headers: bearer(session.bearer) });
  assert.equal(response.status, 410);
});

test("(e) unknown recovery turns return not found and an unwired endpoint is unavailable", async (t) => {
  const registry = fixedRegistry(6);
  const session = createdSession(registry);
  const recovery = new RecoveryStore(registry);
  const server = await liveServer(registry, recovery);
  const unwired = await liveServer(registry);
  t.after(server.close);
  t.after(unwired.close);

  assert.deepEqual(recovery.recover(session.id, "missing"), { ok: false, reason: "NOT_FOUND" });
  const missing = await fetch(`${server.origin}/v1/sessions/${session.id}/turns/missing`, { headers: bearer(session.bearer) });
  const unavailable = await fetch(`${unwired.origin}/v1/sessions/${session.id}/turns/missing`, { headers: bearer(session.bearer) });
  assert.equal(missing.status, 404);
  assert.equal(unavailable.status, 501);
});

function fixedRegistry(value: number): SessionRegistry {
  return new SessionRegistry({ now: () => 0, generationKey: Buffer.alloc(32, value) });
}

function createdSession(registry: SessionRegistry): { readonly id: string; readonly bearer: string } {
  const created = registry.createSession(sessionOptions);
  assert.equal("error" in created, false);
  if ("error" in created) throw new Error("session creation failed");
  return { id: created.handle.id, bearer: created.bearer };
}

function bearer(value: string): HeadersInit {
  return { authorization: `Bearer ${value}` };
}

async function liveServer(registry: SessionRegistry, recovery?: RecoveryStore): Promise<{ readonly origin: string; readonly close: () => Promise<void> }> {
  const server = createServer(registry, recovery === undefined ? {} : { recovery });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("expected TCP listener");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    close: () => new Promise((resolve, reject) => server.close((error) => error === undefined ? resolve() : reject(error))),
  };
}
