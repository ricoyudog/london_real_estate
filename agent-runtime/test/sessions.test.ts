import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import test from "node:test";

import { SessionRegistry, type NewSession } from "../src/sessions.ts";

const sessionOptions = {
  principal: "operator",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["market.read"],
  allowed_refresh_profiles: ["bank-rate"],
};

function registry(now = 1_000, generationKey = Buffer.alloc(32, 7)): SessionRegistry {
  return new SessionRegistry({ now: () => now, generationKey });
}

function created(registryToUse: SessionRegistry): NewSession {
  const result = registryToUse.createSession(sessionOptions);
  if ("error" in result) assert.fail(`unexpected creation failure: ${result.error}`);
  return result;
}

test("a: create returns a one-time bearer and hash-only handle", () => {
  const logged: string[] = [];
  // test-only logging for assertion capture
  const originalLog = console.log;
  console.log = (...values: unknown[]) => logged.push(values.map(String).join(" "));
  let createdSession: NewSession;
  try {
    createdSession = created(registry());
  } finally {
    console.log = originalLog;
  }

  assert.equal(Buffer.from(createdSession.bearer, "base64url").byteLength, 32);
  assert.equal(createdSession.handle.bearer_hash.length, 64);
  assert.equal(createdSession.handle.bearer_hash.includes(createdSession.bearer), false);
  assert.equal(createdSession.expires_at_ms, 1_801_000);
  assert.equal(logged.join("\n").includes(createdSession.bearer), false);
});

test("b: authenticate accepts only the matching bearer", () => {
  const registryToUse = registry();
  const createdSession = created(registryToUse);

  assert.deepEqual(registryToUse.authenticate(createdSession.handle.id, createdSession.bearer), {
    ok: true,
    status: "active",
  });
  assert.deepEqual(registryToUse.authenticate(createdSession.handle.id, "incorrect"), {
    ok: false,
    reason: "UNAUTHENTICATED",
  });
});

test("c: IDs authenticate the current runtime generation", () => {
  const generationKey = Buffer.alloc(32, 3);
  const registryToUse = registry(1_000, generationKey);
  const createdSession = created(registryToUse);

  assert.equal(createdSession.handle.id.split(".")[0], generationKey.toString("base64url"));
  assert.equal(registryToUse.status(createdSession.handle.id), "active");
  assert.equal(registryToUse.isPreRestartId(createdSession.handle.id), false);
});

test("d: malformed and deleted IDs are gone but not pre-restart", () => {
  const registryToUse = registry();
  const createdSession = created(registryToUse);
  registryToUse.close(createdSession.handle.id);

  assert.equal(registryToUse.status(createdSession.handle.id), "gone");
  assert.equal(registryToUse.isPreRestartId(createdSession.handle.id), false);
  assert.equal(registryToUse.status("bad.id.with.parts"), "gone");
  assert.equal(registryToUse.isPreRestartId("bad.id.with.parts"), false);
});

test("e: idle expiry rejects authentication and tombstones scope", () => {
  let now = 1_000;
  const registryToUse = new SessionRegistry({ now: () => now, generationKey: Buffer.alloc(32, 4) });
  const createdSession = created(registryToUse);
  now += 1_800_000;

  assert.equal(registryToUse.status(createdSession.handle.id), "expired");
  assert.deepEqual(registryToUse.authenticate(createdSession.handle.id, createdSession.bearer), {
    ok: false,
    reason: "EXPIRED",
  });
  assert.throws(() => registryToUse.createSession({ ...sessionOptions, explicit_scope_id: createdSession.handle.scope_id }));
});

test("f: touch refreshes the idle deadline only for active sessions", () => {
  let now = 1_000;
  const registryToUse = new SessionRegistry({ now: () => now, generationKey: Buffer.alloc(32, 5) });
  const createdSession = created(registryToUse);
  now += 1_799_999;

  assert.equal(registryToUse.touch(createdSession.handle.id), true);
  now += 1;
  assert.equal(registryToUse.status(createdSession.handle.id), "active");
  now += 1_800_000;
  assert.equal(registryToUse.touch(createdSession.handle.id), false);
});

test("g: eighth session succeeds and ninth reports the session limit", () => {
  const registryToUse = registry();
  for (let index = 0; index < 8; index += 1) created(registryToUse);

  assert.deepEqual(registryToUse.createSession(sessionOptions), { error: "SESSION_LIMIT" });
});

test("h: only one active turn is allowed and releases preserve lifetime count", () => {
  const registryToUse = registry();
  const createdSession = created(registryToUse);

  assert.deepEqual(registryToUse.reserveTurn(createdSession.handle.id), { ok: true });
  assert.deepEqual(registryToUse.reserveTurn(createdSession.handle.id), {
    ok: false,
    reason: "NO_ACTIVE_TURN",
  });
  registryToUse.releaseTurn(createdSession.handle.id);
  for (let index = 1; index < 16; index += 1) {
    assert.deepEqual(registryToUse.reserveTurn(createdSession.handle.id), { ok: true });
    registryToUse.releaseTurn(createdSession.handle.id);
  }
  assert.deepEqual(registryToUse.reserveTurn(createdSession.handle.id), {
    ok: false,
    reason: "TURN_LIMIT",
  });
});

test("i: inactive sessions cannot reserve turns", () => {
  const registryToUse = registry();

  assert.deepEqual(registryToUse.reserveTurn("missing"), { ok: false, reason: "NOT_ACTIVE" });
});

test("j: recovery records have a lifetime limit", () => {
  const registryToUse = registry();
  const createdSession = created(registryToUse);
  for (let index = 0; index < 32; index += 1) {
    assert.deepEqual(registryToUse.recordRecovery(createdSession.handle.id), { ok: true });
  }

  assert.deepEqual(registryToUse.recordRecovery(createdSession.handle.id), {
    ok: false,
    reason: "RECOVERY_LIMIT",
  });
});

test("k: close tombstones the scope and removes session metadata", () => {
  const registryToUse = registry();
  const createdSession = created(registryToUse);
  registryToUse.close(createdSession.handle.id);

  assert.equal(registryToUse.getSession(createdSession.handle.id), undefined);
  assert.throws(() => registryToUse.createSession({ ...sessionOptions, explicit_scope_id: createdSession.handle.scope_id }));
});

test("l: concurrent same-scope creation permits at most one success", async () => {
  const registryToUse = registry();
  const scope = `scope_${randomBytes(24).toString("base64url")}`;
  await new Promise<void>((resolve) => setImmediate(resolve));
  const results = await Promise.allSettled([
    Promise.resolve().then(() => registryToUse.createSession({ ...sessionOptions, explicit_scope_id: scope })),
    Promise.resolve().then(() => registryToUse.createSession({ ...sessionOptions, explicit_scope_id: scope })),
  ]);

  assert.equal(results.filter((result) => result.status === "fulfilled").length, 1);
  assert.equal(results.filter((result) => result.status === "rejected").length, 1);
});

test("m: replacement registry recognizes a valid old-generation ID as pre-restart", () => {
  const original = registry(1_000, Buffer.alloc(32, 8));
  const createdSession = created(original);
  const replacement = registry(1_000, Buffer.alloc(32, 9));

  assert.equal(replacement.status(createdSession.handle.id), "gone");
  assert.equal(replacement.isPreRestartId(createdSession.handle.id), true);
});
