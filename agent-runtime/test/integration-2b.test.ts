import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import * as http from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";

import { fauxAssistantMessage, fauxText, fauxToolCall, type FauxResponseStep } from "@earendil-works/pi-ai";

import { createApp, type App } from "../src/app.ts";
import type { AgentEventV1 } from "../src/sse.ts";
import { createFauxModels, FAUX_MODEL_REF } from "./helpers/faux-models.ts";
import { fixtureAssets } from "./fixtures/fixture-assets.ts";
import { RecordingLauncher } from "./fixtures/shared.ts";

const worktreeRoot = resolve(import.meta.dirname, "../..");
const evidenceRoot = join(import.meta.dirname, ".evidence/integration-2b");
const context = {
  principal: "integration-agent", capability_scope_id: "transport-scope", allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current"], allowed_refresh_profiles: ["bank-rate-latest"],
};

test("full HTTP/SSE session preserves order, replay, recovery, and bearer secrecy", async (t) => {
  const fixture = await setup("happy", false);
  t.after(fixture.close);
  const session = await createSession(fixture.origin);
  const live = openSse(fixture.origin, session, null);
  await live.connected;

  const turn = await postMessage(fixture.origin, session, "Give the latest Bank Rate.");
  const events = await live.until((items) => terminal(items, turn));
  assert.deepEqual(events.filter((event) => event.turn_id === turn).map((event) => event.type), [
    "turn.started", "tool.started", "tool.completed", "tool.started", "tool.completed",
    "tool.started", "tool.completed", "artifact.final", "turn.completed",
  ]);
  assert.equal(events[0]?.type, "session.started");
  const started = events.find((event) => event.turn_id === turn && event.type === "turn.started");
  assert.deepEqual(started?.payload, { runtime_engine: "pi-agent-session", model: FAUX_MODEL_REF });
  const artifact = artifactFrom(events);
  assertArtifact(artifact, false);

  const late = openSse(fixture.origin, session, null);
  const lateEvents = await late.until((items) => terminal(items, turn), false);
  assert.deepEqual(lateEvents.map((event) => event.sequence), events.map((event) => event.sequence));
  fixture.faux.faux.setResponses([...scriptedTurn(fixture.launcher)]);
  const liveTailTurn = await postMessage(fixture.origin, session, "Give the current Bank Rate.");
  const liveTail = await late.until((items) => terminal(items, liveTailTurn));
  assert.ok(liveTail.some((event) => event.turn_id === liveTailTurn && event.type === "turn.started"));
  const beforeArtifact = events[events.findIndex((event) => event.type === "artifact.final") - 1];
  assert.ok(beforeArtifact);
  const replay = openSse(fixture.origin, session, beforeArtifact.event_id);
  const replayEvents = await replay.until((items) => terminal(items, turn));
  assert.deepEqual(replayEvents.map((event) => event.type), ["artifact.final", "turn.completed"]);

  const recovered = await getJson(`${fixture.origin}/v1/sessions/${session.id}/turns/${turn}`, session);
  assert.equal(recovered["state"], "completed");
  assertArtifact(record(recovered["artifact"])["artifact"], false);
  assert.equal(JSON.stringify({ events, lateEvents, replayEvents, recovered }).includes(session.bearer), false);
  assert.equal(fixture.logs.join("\n").includes(session.bearer), false);
  assert.deepEqual(record(fixture.traces[0])["tool_sequence"], ["query_market_data", "get_citation_metadata", "finalize_market_brief"]);
  assert.equal(record(fixture.traces[0])["terminal_state"], "completed");
  assert.equal(record(fixture.traces[0])["runtime_engine"], "pi-agent-session");
  assert.equal(JSON.stringify(fixture.traces).includes("Give the latest Bank Rate."), false);
  assert.equal(JSON.stringify(fixture.traces).includes(session.bearer), false);
  writeEvidence("happy.json", { events, lateEvents, liveTail, replayEvents, recovered });
});

test("cancel and conflict are observable through the real transport and release the session", async (t) => {
  const gate = deferred<void>();
  const fixture = await setup("cancel", false, gate.promise);
  t.after(fixture.close);
  const session = await createSession(fixture.origin);
  const stream = openSse(fixture.origin, session, null);
  await stream.connected;
  const turn = await postMessage(fixture.origin, session, "Give the latest Bank Rate.");
  await waitFor(() => fixture.app.hub.events(session.id).some((event) => event.type === "turn.started"));
  const conflict = await fetch(`${fixture.origin}/v1/sessions/${session.id}/messages`, { method: "POST", headers: auth(session), body: JSON.stringify({ message: "Give the current rate." }) });
  assert.equal(conflict.status, 409);
  const cancelled = await fetch(`${fixture.origin}/v1/sessions/${session.id}/turns/${turn}/cancel`, { method: "POST", headers: auth(session) });
  assert.equal(cancelled.status, 202);
  gate.resolve();
  const events = await stream.until((items) => terminal(items, turn));
  assert.equal(events.filter((event) => event.turn_id === turn && event.type === "turn.completed").length, 1);
  assert.equal(events.some((event) => event.turn_id === turn && event.type === "artifact.final"), false);
  assert.equal(record(events.find((event) => event.turn_id === turn && event.type === "turn.completed")?.payload)["terminal_state"], "cancelled");
  const cancelledTerminal = events.find((event) => event.turn_id === turn && event.type === "turn.completed");
  assert.ok(cancelledTerminal);
  const replayAfterCancel = openSse(fixture.origin, session, cancelledTerminal.event_id);
  await replayAfterCancel.connected;
  fixture.faux.faux.setResponses([...scriptedTurn(fixture.launcher)]);
  const nextTurn = await postMessage(fixture.origin, session, "Give the current Bank Rate.");
  const resumed = await replayAfterCancel.until((items) => terminal(items, nextTurn));
  assert.ok(resumed.some((event) => event.turn_id === nextTurn && event.type === "turn.completed"));
  assert.equal(fixture.logs.join("\n").includes(session.bearer), false);
  writeEvidence("cancel.json", { events, resumed });
});

test("published_at null and distinct confidence survive live, replay, and recovery", async (t) => {
  const fixture = await setup("published-null", true);
  t.after(fixture.close);
  const session = await createSession(fixture.origin);
  const stream = openSse(fixture.origin, session, null);
  await stream.connected;
  const turn = await postMessage(fixture.origin, session, "Give the latest Bank Rate.");
  const live = await stream.until((items) => terminal(items, turn));
  const artifact = artifactFrom(live);
  assertArtifact(artifact, true);
  const beforeArtifact = live[live.findIndex((event) => event.type === "artifact.final") - 1];
  assert.ok(beforeArtifact);
  const replay = openSse(fixture.origin, session, beforeArtifact.event_id);
  const replayed = await replay.until((items) => terminal(items, turn));
  assertArtifact(artifactFrom(replayed), true);
  const recovered = await getJson(`${fixture.origin}/v1/sessions/${session.id}/turns/${turn}`, session);
  assertArtifact(record(recovered["artifact"])["artifact"], true);
  assert.equal(fixture.logs.join("\n").includes(session.bearer), false);
  writeEvidence("published-null.json", { live, replayed, recovered });
});

test("runtime startup failures emit only the safe RUNTIME_UNAVAILABLE reason", async (t) => {
  const dataDir = join(mkdtempSync(join(tmpdir(), "integration-runtime-failure-")), "data");
  execFileSync("uv", ["run", "cre", "--data-dir", dataDir, "db", "migrate"], { cwd: worktreeRoot });
  const faux = createFauxModels([]);
  process.env.PI_MODEL = FAUX_MODEL_REF;
  const traces: unknown[] = [];
  const app = await createApp({
    ctx: context,
    creDataDir: dataDir,
    modelsOverride: faux.models,
    createSession: async () => { throw new Error("sensitive runtime detail"); },
    trace: (entry) => { traces.push(entry); },
  });
  t.after(() => app.close());
  const created = app.registry.createSession({ principal: "browser", allowed_access_classes: ["open"], allowed_capability_ids: ["uk.bank-rate-current"], allowed_refresh_profiles: [] });
  assert.equal("error" in created, false);
  if ("error" in created) throw new Error("session creation failed");

  await assert.rejects(app.runTurnForSession(created.handle.id, "secret prompt"));

  const failed = app.hub.events(created.handle.id).find((event) => event.type === "turn.failed");
  assert.deepEqual(failed?.payload, { reason_code: "RUNTIME_UNAVAILABLE" });
  assert.equal(JSON.stringify(failed).includes("sensitive runtime detail"), false);
  assert.equal(JSON.stringify(traces).includes("secret prompt"), false);
  assert.equal(record(traces[0])["reason_code"], "RUNTIME_UNAVAILABLE");
});

async function setup(name: string, publishedNull: boolean, firstResponseGate?: Promise<void>) {
  const logs: string[] = [];
  const traces: unknown[] = [];
  // test-only logging for assertion capture
  const originalLog = console.log;
  const originalWarn = console.warn;
  const originalError = console.error;
  const capture = (...values: readonly unknown[]) => { logs.push(values.map(String).join(" ")); };
  console.log = capture;
  console.warn = capture;
  console.error = capture;
  const dataDir = join(mkdtempSync(join(tmpdir(), `integration-2b-${name}-`)), "data");
  execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_bank_rate.py", dataDir, "5.25", ...(publishedNull ? ["--published-null"] : [])], { cwd: worktreeRoot });
  const launcher = new RecordingLauncher(dataDir);
  const script = scriptedTurn(launcher, firstResponseGate);
  const faux = createFauxModels(script);
  process.env.PI_MODEL = FAUX_MODEL_REF;
  const app = await createApp({
    ctx: context, creDataDir: dataDir, assetsDir: fixtureAssets(worktreeRoot), launcher, modelsOverride: faux.models,
    trace: (entry) => { traces.push(entry); },
  });
  await new Promise<void>((resolve) => app.server.listen(0, "127.0.0.1", resolve));
  const address = app.server.address();
  assert.ok(address !== null && typeof address !== "string");
  return {
    app, launcher, faux, logs, traces, origin: `http://127.0.0.1:${address.port}`,
    close: async () => {
      try { await app.close(); }
      finally { console.log = originalLog; console.warn = originalWarn; console.error = originalError; }
    },
  };
}

function scriptedTurn(launcher: RecordingLauncher, gate?: Promise<void>): readonly FauxResponseStep[] {
  const query = fauxAssistantMessage(fauxToolCall("query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics", limit: 1 }), { stopReason: "toolUse" });
  return [
    gate === undefined ? query : async () => { await gate; return query; },
    () => fauxAssistantMessage(fauxToolCall("get_citation_metadata", { citation_refs: queryRecord(launcher)["citation_refs"] }), { stopReason: "toolUse" }),
    () => {
      const refs = queryRecord(launcher)["citation_refs"];
      assert.ok(Array.isArray(refs) && typeof refs[0] === "string");
      return fauxAssistantMessage(fauxToolCall("finalize_market_brief", {
        title: "Bank Rate brief", status: "complete",
        facts: [{ claim_id: "bank-rate", kind: "numeric", confidence: "medium", numeric_citation_ref: refs[0] }],
        inferences: [{ claim_id: "outlook", text: "Direction remains uncertain.", confidence: "low", supporting_fact_ids: ["bank-rate"], caveat: "Conditions may change." }],
        limitations: [],
      }), { stopReason: "toolUse" });
    },
    fauxAssistantMessage(fauxText("Brief ready.")),
  ];
}

function queryRecord(launcher: RecordingLauncher): Readonly<Record<string, unknown>> {
  const records = launcher.calls.filter((call) => call.toolName === "query_market_data").at(-1)?.result.data?.["records"];
  assert.ok(Array.isArray(records) && records[0] !== undefined);
  return record(records[0]);
}

type Session = { readonly id: string; readonly bearer: string };
async function createSession(origin: string): Promise<Session> { const response = await fetch(`${origin}/v1/sessions`, { method: "POST" }); assert.equal(response.status, 201); return record(await response.json()) as Session; }
async function postMessage(origin: string, session: Session, message: string): Promise<string> { const response = await fetch(`${origin}/v1/sessions/${session.id}/messages`, { method: "POST", headers: { ...auth(session), "content-type": "application/json" }, body: JSON.stringify({ message }) }); assert.equal(response.status, 202); const value = record(await response.json()); assert.equal(typeof value["turn_id"], "string"); return value["turn_id"] as string; }
async function getJson(url: string, session: Session): Promise<Readonly<Record<string, unknown>>> { const response = await fetch(url, { headers: auth(session) }); assert.equal(response.status, 200); return record(await response.json()); }
function auth(session: Session): Record<string, string> { return { authorization: `Bearer ${session.bearer}` }; }

function openSse(origin: string, session: Session, lastEventId: string | null) {
  const events: AgentEventV1[] = [];
  const waiters = new Set<() => void>();
  let connectedResolve: () => void = () => undefined;
  const connected = new Promise<void>((resolve) => { connectedResolve = resolve; });
  const request = http.get(`${origin}/v1/sessions/${session.id}/events`, { headers: { ...auth(session), ...(lastEventId === null ? {} : { "last-event-id": lastEventId }) } }, (response) => {
    connectedResolve();
    let text = "";
    response.setEncoding("utf8");
    response.on("data", (chunk: string) => {
      text += chunk;
      const frames = text.split("\n\n");
      text = frames.pop() ?? "";
      for (const frame of frames) {
        const data = frame.split("\n").find((line) => line.startsWith("data: "));
        if (data !== undefined) events.push(JSON.parse(data.slice(6)) as AgentEventV1);
      }
      for (const wake of waiters) wake();
    });
  });
  return { connected, until: (predicate: (items: readonly AgentEventV1[]) => boolean, close = true) => new Promise<readonly AgentEventV1[]>((resolve, reject) => {
    const timeout = setTimeout(() => { request.destroy(); reject(new Error("SSE deadline exceeded")); }, 15_000);
    const check = () => { if (!predicate(events)) return; clearTimeout(timeout); waiters.delete(check); if (close) request.destroy(); resolve([...events]); };
    waiters.add(check); check();
  }) };
}

function terminal(events: readonly AgentEventV1[], turnId: string): boolean { return events.some((event) => event.turn_id === turnId && (event.type === "turn.completed" || event.type === "turn.failed")); }
function artifactFrom(events: readonly AgentEventV1[]): unknown { return record(events.find((event) => event.type === "artifact.final")?.payload)["artifact"]; }
function assertArtifact(value: unknown, publishedNull: boolean): void { const artifact = record(value); const facts = artifact["facts"]; assert.ok(Array.isArray(facts)); assert.equal(record(facts[0])["numeric_value"], "5.25"); const sources = artifact["sources"]; assert.ok(Array.isArray(sources)); assert.match(String(record(sources[0])["public_url"]), /^https:\/\//); assert.equal(typeof artifact["lineage"], "object"); assert.equal(artifact["published_at"], publishedNull ? null : "2026-08-01T09:00:00.000000Z"); assert.equal(artifact["publication_date_warning"], publishedNull); assert.equal(record(artifact["datasource_confidence"])["bank-rate"], "high"); assert.equal(record(artifact["fact_confidence"])["bank-rate"], "medium"); assert.equal(record(artifact["inference_confidence"])["outlook"], "low"); if (publishedNull) assert.match(String(artifact["display_text"]), /Publication date unavailable/); }
function record(value: unknown): Readonly<Record<string, unknown>> { assert.ok(isRecord(value)); return value; }
function isRecord(value: unknown): value is Readonly<Record<string, unknown>> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function writeEvidence(name: string, value: unknown): void { mkdirSync(evidenceRoot, { recursive: true }); writeFileSync(join(evidenceRoot, name), `${JSON.stringify(value, null, 2)}\n`); }
function deferred<T>() { let resolve!: (value: T | PromiseLike<T>) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }
async function waitFor(predicate: () => boolean): Promise<void> { const deadline = performance.now() + 5_000; while (!predicate()) { if (performance.now() >= deadline) throw new Error("condition deadline exceeded"); await new Promise<void>((resolve) => setImmediate(resolve)); } }
