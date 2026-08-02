import assert from "node:assert/strict";
import test from "node:test";

import {
  BudgetExceeded,
  LifecycleReducer,
  RefreshArgsChanged,
  StateTransitionError,
  TurnContext,
  TurnDeadlineExceeded,
  createSessionRuntime,
  defaultTurnLimits,
} from "../src/runtime.ts";

const session = (capability_scope_id: string) => ({
  principal: "principal",
  capability_scope_id,
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current"],
  allowed_refresh_profiles: ["bank-rate-latest"],
});

const context = (now = 0) => {
  let id = 0;
  return new TurnContext(session("scope_a"), defaultTurnLimits, {
    now: () => now,
    idFactory: () => `fixed-${++id}`,
  });
};

test("a: session runtimes retain distinct scope contexts", async () => {
  // Given: two independent session runtimes
  const first = createSessionRuntime(session("scope_a"));
  const second = createSessionRuntime(session("scope_b"));

  // When: each runs its first turn
  await Promise.all([first.runTurn("first"), second.runTurn("second")]);

  // Then: their turn contexts retain their own scopes
  assert.equal(first.getActiveTurnContext()?.session.capability_scope_id, "scope_a");
  assert.equal(second.getActiveTurnContext()?.session.capability_scope_id, "scope_b");
});

test("b: runtime returns the same active turn context", async () => {
  // Given: a new runtime
  const runtime = createSessionRuntime(session("scope_a"));

  // When: its active turn is created
  await runtime.runTurn("first");
  const active = runtime.getActiveTurnContext();

  // Then: it cannot be replaced through the public runtime contract
  assert.equal(runtime.getActiveTurnContext(), active);
});

test("c: refresh retries with unchanged args reuse request identity", () => {
  // Given: one turn context and one logical tool call
  const turn = context();

  // When: the call is retried with identical args
  const initial = turn.registerRefreshRequest("turn", "call", { postcode: "EC2Y" });
  const retry = turn.registerRefreshRequest("turn", "call", { postcode: "EC2Y" });

  // Then: the same refresh request ID is retained
  assert.equal(retry, initial);
});

test("d: refresh retries with changed args are rejected", () => {
  // Given: a registered logical refresh
  const turn = context();
  turn.registerRefreshRequest("turn", "call", { postcode: "EC2Y" });

  // When/Then: a changed retry cannot reuse the identity
  assert.throws(
    () => turn.registerRefreshRequest("turn", "call", { postcode: "W1A" }),
    RefreshArgsChanged,
  );
});

test("e: separate refresh calls receive separate identities", () => {
  // Given: one turn context
  const turn = context();

  // When: two tool call IDs request refreshes
  const first = turn.registerRefreshRequest("turn", "call_a", { postcode: "EC2Y" });
  const second = turn.registerRefreshRequest("turn", "call_b", { postcode: "EC2Y" });

  // Then: the IDs differ even for equivalent arguments
  assert.notEqual(first, second);
});

test("f: lifecycle reducer rejects a second terminal event", () => {
  // Given: a reducer with a final artifact and completed turn
  const reducer = new LifecycleReducer();
  reducer.transition({ type: "turn.started" });
  reducer.transition({ type: "artifact.final" });
  reducer.transition({ type: "turn.completed", terminal_state: "completed" });

  // When/Then: another terminal event is rejected
  assert.throws(() => reducer.transition({ type: "turn.failed", reason_code: "RUNTIME_UNAVAILABLE" }), StateTransitionError);
});

test("g: lifecycle reducer requires final artifact before successful completion", () => {
  // Given: a running reducer
  const reducer = new LifecycleReducer();
  reducer.transition({ type: "turn.started" });

  // When/Then: successful completion cannot precede its artifact
  assert.throws(
    () => reducer.transition({ type: "turn.completed", terminal_state: "completed" }),
    StateTransitionError,
  );
});

test("lifecycle reducer allows cancelled terminal without artifact", () => {
  // Given: a started turn without a final artifact
  const reducer = new LifecycleReducer();
  reducer.transition({ type: "turn.started" });

  // When: cancellation reaches the terminal event
  reducer.transition({ type: "turn.completed", terminal_state: "cancelled" });

  // Then: it terminates without fabricating an artifact
  assert.equal(reducer.state(), "cancelled");
  assert.equal(reducer.events().filter((event) => event.type === "turn.completed").length, 1);
});

test("lifecycle reducer allows failed terminal without artifact", () => {
  // Given: a started turn without a final artifact
  const reducer = new LifecycleReducer();
  reducer.transition({ type: "turn.started" });

  // When: it reaches the failed terminal event
  reducer.transition({ type: "turn.failed", reason_code: "RUNTIME_UNAVAILABLE" });

  // Then: it records failure without fabricating an artifact
  assert.equal(reducer.state(), "failed");
});

test("lifecycle reducer still rejects completed terminal without artifact", () => {
  // Given: a started turn without a final artifact
  const reducer = new LifecycleReducer();
  reducer.transition({ type: "turn.started" });

  // When/Then: successful completion is still rejected
  assert.throws(
    () => reducer.transition({ type: "turn.completed", terminal_state: "completed" }),
    StateTransitionError,
  );
});

test("h: concurrent terminal emission applies exactly once", async () => {
  // Given: a turn that has its final artifact
  const reducer = new LifecycleReducer();
  reducer.transition({ type: "turn.started" });
  reducer.transition({ type: "artifact.final" });
  let release!: () => void;
  const barrier = new Promise<void>((resolve) => {
    release = resolve;
  });

  // When: two queued tasks emit terminal events after one barrier
  const attempt = async (event: { readonly type: "turn.completed"; readonly terminal_state: "completed" }) => {
    await barrier;
    try {
      reducer.transition(event);
      return "applied";
    } catch (error) {
      assert.ok(error instanceof StateTransitionError);
      return "rejected";
    }
  };
  const results = Promise.all([
    attempt({ type: "turn.completed", terminal_state: "completed" }),
    attempt({ type: "turn.completed", terminal_state: "completed" }),
  ]);
  release();

  // Then: exactly one terminal transition was recorded
  assert.deepEqual(await results, ["applied", "rejected"]);
  assert.equal(reducer.events().filter((event) => event.type === "turn.completed").length, 1);
});

test("i: turn budgets and monotonic deadline are enforced", () => {
  // Given: a context with the fixed limits
  const turn = context();

  // When/Then: each per-kind budget stops the next call
  for (let count = 0; count < 8; count += 1) turn.beforeToolCall("query_market_data", {});
  assert.throws(() => turn.beforeToolCall("query_market_data", {}), BudgetExceeded);

  const polls = context();
  for (let count = 0; count < 3; count += 1) polls.beforeToolCall("get_refresh_status", {});
  assert.throws(() => polls.beforeToolCall("get_refresh_status", {}), BudgetExceeded);

  const finalizers = context();
  for (let count = 0; count < 2; count += 1) finalizers.beforeToolCall("finalize_market_brief", {});
  assert.throws(() => finalizers.beforeToolCall("finalize_market_brief", {}), BudgetExceeded);

  let now = 0;
  const expired = new TurnContext(session("scope_a"), defaultTurnLimits, { now: () => now });
  now = defaultTurnLimits.turnDeadlineMs;
  assert.throws(() => expired.beforeToolCall("query_market_data", {}), TurnDeadlineExceeded);
});

test("j: model-visible tool bytes have a cumulative budget", () => {
  // Given: a fresh turn context
  const turn = context();

  // When: the byte cap is consumed and exceeded
  turn.beforeToolCall("query_market_data", { modelToolBytes: 128 * 1024 });

  // Then: one additional byte is rejected
  assert.throws(
    () => turn.beforeToolCall("query_market_data", { modelToolBytes: 1 }),
    BudgetExceeded,
  );
});
