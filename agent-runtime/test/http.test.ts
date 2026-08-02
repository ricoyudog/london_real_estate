import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import test from "node:test";

import { createServer } from "../src/http.ts";
import { SessionRegistry } from "../src/sessions.ts";

const sessionOptions = {
  principal: "operator",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["market.read"],
  allowed_refresh_profiles: ["bank-rate"],
};

type LiveServer = {
  readonly registry: SessionRegistry;
  readonly origin: string;
  readonly close: () => Promise<void>;
};

async function liveServer(generationKey = Buffer.alloc(32, 7)): Promise<LiveServer> {
  const registry = new SessionRegistry({ generationKey });
  const server = createServer(registry);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as AddressInfo;
  return { registry, origin: `http://127.0.0.1:${address.port}`, close: () => closeServer(server) };
}

function closeServer(server: Server): Promise<void> {
  return new Promise((resolve, reject) => server.close((error) => error === undefined ? resolve() : reject(error)));
}

async function createSession(server: LiveServer) {
  const response = await fetch(`${server.origin}/v1/sessions`, { method: "POST" });
  assert.equal(response.status, 201);
  return JSON.parse(await response.text());
}

function authorized(bearer: string): HeadersInit {
  return { authorization: `Bearer ${bearer}` };
}

function sessionFrom(result: ReturnType<SessionRegistry["createSession"]>) {
  assert.equal("error" in result, false);
  if ("error" in result) throw new Error("session creation failed");
  return result;
}

test("(a) POST sessions returns a one-time bearer capability", async (t) => {
  const server = await liveServer();
  t.after(server.close);

  const response = await fetch(`${server.origin}/v1/sessions`, { method: "POST" });
  const body = JSON.parse(await response.text());

  assert.equal(response.status, 201);
  assert.equal(typeof body.id, "string");
  assert.equal(typeof body.scope_id, "string");
  assert.equal(Buffer.from(body.bearer, "base64url").byteLength, 32);
});

test("(b) authenticated routes return their documented placeholders", async (t) => {
  const server = await liveServer();
  t.after(server.close);
  const session = await createSession(server);
  const headers = authorized(session.bearer);

  const message = await fetch(`${server.origin}/v1/sessions/${session.id}/messages`, { method: "POST", headers });
  assert.equal(message.status, 202);
  const turnId = JSON.parse(await message.text()).turn_id;
  assert.equal(typeof turnId, "string");

  const turn = await fetch(`${server.origin}/v1/sessions/${session.id}/turns/${turnId}`, { headers });
  const missingTurn = await fetch(`${server.origin}/v1/sessions/${session.id}/turns/missing`, { headers });
  assert.equal(turn.status, 501);
  assert.equal(missingTurn.status, 501);

  const events = await fetch(`${server.origin}/v1/sessions/${session.id}/events`, { headers });
  assert.equal(events.status, 200);
  assert.equal(events.headers.get("content-type"), "text/event-stream");

  const approval = await fetch(`${server.origin}/v1/sessions/${session.id}/approvals/approval`, { method: "POST", headers });
  assert.equal(approval.status, 501);
  assert.deepEqual(JSON.parse(await approval.text()), { error: "NOT_IMPLEMENTED" });

  const cancel = await fetch(`${server.origin}/v1/sessions/${session.id}/turns/${turnId}/cancel`, { method: "POST", headers });
  const missingCancel = await fetch(`${server.origin}/v1/sessions/${session.id}/turns/missing/cancel`, { method: "POST", headers });
  assert.equal(cancel.status, 202);
  assert.equal(missingCancel.status, 404);
});

test("(c) missing or invalid bearer is rejected", async (t) => {
  const server = await liveServer();
  t.after(server.close);
  const session = await createSession(server);

  const missing = await fetch(`${server.origin}/v1/sessions/${session.id}/events`);
  const invalid = await fetch(`${server.origin}/v1/sessions/${session.id}/events`, { headers: authorized("invalid") });

  assert.equal(missing.status, 401);
  assert.equal(invalid.status, 401);
});

test("(d) unknown current-generation session returns 404", async (t) => {
  const server = await liveServer();
  t.after(server.close);
  const id = `${Buffer.alloc(32, 7).toString("base64url")}.${Buffer.alloc(16, 8).toString("base64url")}`;

  const response = await fetch(`${server.origin}/v1/sessions/${id}/events`, { headers: authorized("invalid") });

  assert.equal(response.status, 404);
});

test("(e) pre-restart session returns 410", async (t) => {
  const oldRegistry = new SessionRegistry({ generationKey: Buffer.alloc(32, 8) });
  const oldSession = sessionFrom(oldRegistry.createSession(sessionOptions));
  const server = await liveServer(Buffer.alloc(32, 9));
  t.after(server.close);

  const response = await fetch(`${server.origin}/v1/sessions/${oldSession.handle.id}/events`, { headers: authorized(oldSession.bearer) });

  assert.equal(response.status, 410);
});

test("(f) an active turn conflicts with a new message", async (t) => {
  const server = await liveServer();
  t.after(server.close);
  const session = await createSession(server);
  assert.deepEqual(server.registry.reserveTurn(session.id), { ok: true });

  const response = await fetch(`${server.origin}/v1/sessions/${session.id}/messages`, { method: "POST", headers: authorized(session.bearer) });

  assert.equal(response.status, 409);
  assert.deepEqual(JSON.parse(await response.text()), { error: "active_turn" });
});

test("(g) DELETE disposes a session without external state", async (t) => {
  const server = await liveServer();
  t.after(server.close);
  const session = await createSession(server);

  const deleted = await fetch(`${server.origin}/v1/sessions/${session.id}`, { method: "DELETE", headers: authorized(session.bearer) });
  const reused = await fetch(`${server.origin}/v1/sessions/${session.id}/messages`, { method: "POST", headers: authorized(session.bearer) });

  assert.equal(deleted.status, 204);
  assert.equal(server.registry.getSession(session.id), undefined);
  assert.equal(reused.status, 404);
});

test("(h) unknown routes return 404 and known routes reject wrong methods", async (t) => {
  const server = await liveServer();
  t.after(server.close);

  const unknown = await fetch(`${server.origin}/v1/not-a-route`);
  const malformed = await fetch(`${server.origin}/v1/sessions/%ZZ/events`, { headers: authorized("invalid") });
  const wrongMethod = await fetch(`${server.origin}/v1/sessions`);

  assert.equal(unknown.status, 404);
  assert.equal(malformed.status, 404);
  assert.equal(wrongMethod.status, 405);
});
