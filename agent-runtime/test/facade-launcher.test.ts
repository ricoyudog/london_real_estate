import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, copyFileSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  FacadeLauncher,
  LauncherConfigError,
  MAX_STDIN_BYTES,
} from "../src/facade-launcher.ts";

const runtimeRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const worktreeRoot = resolve(runtimeRoot, "..");
const helperSource = join(runtimeRoot, "test/helpers/facade-child.mjs");

function request(requestId: string, argumentsValue: Readonly<Record<string, unknown>> = {}) {
  return {
    schema_version: "agent_tool_request.v1",
    request_id: requestId,
    arguments: argumentsValue,
    host_context: {
      principal: "test",
      capability_scope_id: `scope_${requestId}`,
      turn_id: "turn_test",
      tool_call_id: "tc_test",
      allowed_access_classes: ["open"],
      allowed_capability_ids: ["uk.bank-rate-current"],
      allowed_refresh_profiles: ["bank-rate-latest"],
    },
  };
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  assert.ok(typeof value === "object" && value !== null && !Array.isArray(value));
  return value as Readonly<Record<string, unknown>>;
}

function migratedStore(): string {
  const dataDir = join(mkdtempSync(join(tmpdir(), "facade-store-")), "seed-data");
  execFileSync("uv", ["run", "cre", "--data-dir", dataDir, "db", "migrate"], {
    cwd: worktreeRoot,
  });
  return dataDir;
}

function seededBankRateStore(): string {
  const dataDir = join(mkdtempSync(join(tmpdir(), "facade-bank-rate-")), "seed-data");
  execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_bank_rate.py", dataDir, "5.25"], {
    cwd: worktreeRoot,
  });
  return dataDir;
}

function withHelper(dataDir: string, kill?: typeof process.kill): FacadeLauncher {
  const helper = join(mkdtempSync(join(tmpdir(), "facade-bin-")), "nan-fung-agent-tools");
  copyFileSync(helperSource, helper);
  chmodSync(helper, 0o700);
  return new FacadeLauncher({ creDataDir: dataDir, binaryPath: helper, ...(kill === undefined ? {} : { kill }) });
}

async function waitForGone(pids: readonly number[]): Promise<void> {
  const deadline = performance.now() + 5_000;
  while (performance.now() < deadline) {
    if (pids.every((pid) => {
      try { process.kill(pid, 0); return false; } catch { return true; }
    })) return;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 50));
  }
  assert.fail(`processes survived: ${pids.join(",")}`);
}

async function waitForPidFile(path: string): Promise<readonly number[]> {
  const deadline = performance.now() + 4_000;
  while (performance.now() < deadline) {
    try {
      const pids = readFileSync(path, "utf8").trim().split("\n").map(Number);
      if (pids.length > 0 && pids.every((pid) => Number.isInteger(pid) && pid > 0)) return pids;
    } catch (error) {
      if (!(error instanceof Error && "code" in error && error.code === "ENOENT")) throw error;
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 25));
  }
  assert.fail(`helper did not become ready: ${path}`);
}

test("a/b: real facade uses the explicit migrated store from an unrelated parent cwd", async () => {
  const dataDir = migratedStore();
  const oldCwd = process.cwd();
  process.chdir(tmpdir());
  try {
    const launcher = new FacadeLauncher({ creDataDir: dataDir });
    const described = await launcher.invoke("describe_market_data", request("call_describe"));
    const queried = await launcher.invoke("query_market_data", request("call_query", {
      capability_id: "uk.bank-rate-current", query_kind: "metrics",
    }));
    assert.equal(described.status, "ok");
    assert.equal(queried.status, "ok");
    assert.deepEqual(queried.data?.records, []);
  } finally {
    process.chdir(oldCwd);
  }
});

test("a.1: real facade accepts the seeded canonical citation locator", async () => {
  const launcher = new FacadeLauncher({ creDataDir: seededBankRateStore() });
  const queryRequest = request("call_seeded_query", {
    capability_id: "uk.bank-rate-current", query_kind: "metrics",
  });
  queryRequest.host_context.capability_scope_id = "scope_seeded_citation";
  const query = await launcher.invoke("query_market_data", queryRequest);
  assert.equal(query.status, "ok");
  const records = query.data?.["records"];
  assert.ok(Array.isArray(records) && records.length === 1);
  const citationRefs = record(records[0])["citation_refs"];
  assert.ok(Array.isArray(citationRefs) && typeof citationRefs[0] === "string");

  const citationRequest = request("call_seeded_citation", { citation_refs: [citationRefs[0]] });
  citationRequest.host_context.capability_scope_id = "scope_seeded_citation";
  const citation = await launcher.invoke("get_citation_metadata", citationRequest);

  assert.equal(citation.status, "ok");
  assert.equal(Array.isArray(citation.data?.["citations"]) && citation.data?.["citations"].length, 1);
});

test("c: FD3 is exactly 32 bytes and its key is absent from argv, env, and stdin", async () => {
  const dataDir = migratedStore();
  const result = await withHelper(dataDir).invoke("describe_market_data", request("call_key"));
  const observation = JSON.parse(readFileSync(join(dataDir, "key-observation.json"), "utf8"));
  assert.equal(result.status, "ok");
  assert.deepEqual(observation, {
    fd3_bytes: 32,
    key_absent_from_argv: true,
    key_absent_from_env: true,
    key_absent_from_stdin: true,
    key_absent_from_stdout: true,
    key_absent_from_stderr: true,
  });
});

test("d: oversized stdin becomes a typed invalid-argument result", async () => {
  const result = await withHelper(migratedStore()).invoke("query_market_data", request("call_large", {
    capability_id: "x".repeat(MAX_STDIN_BYTES), query_kind: "metrics",
  }));
  assert.equal(result.error?.code, "INVALID_ARGUMENT");
});

test("e: stdout overflow becomes typed and kills the process group", async () => {
  const dataDir = migratedStore();
  const result = await withHelper(dataDir).invoke("describe_market_data", request("call_stdout_overflow"));
  assert.equal(result.error?.code, "RESULT_TOO_LARGE");
  await waitForGone(readFileSync(join(dataDir, "call_stdout_overflow.pids"), "utf8").trim().split("\n").map(Number));
});

test("f: timeout applies the bounded group cleanup", async () => {
  const dataDir = migratedStore();
  const startedAt = performance.now();
  const result = await withHelper(dataDir).invoke("describe_market_data", request("call_timeout"));
  assert.equal(result.error?.code, "TIMEOUT");
  assert.ok(performance.now() - startedAt >= 10_000);
  await waitForGone(readFileSync(join(dataDir, "call_timeout.pids"), "utf8").trim().split("\n").map(Number));
});

test("g: cancellation applies the same bounded group cleanup", async () => {
  const dataDir = migratedStore();
  const controller = new AbortController();
  const invocation = withHelper(dataDir).invoke("describe_market_data", request("call_cancel"), { cancelEvent: controller.signal });
  const pids = await waitForPidFile(join(dataDir, "call_cancel.pids"));
  controller.abort();
  const result = await invocation;
  assert.equal(result.error?.code, "TIMEOUT");
  await waitForGone(pids);
});

test("g.1: post-SIGKILL liveness EPERM does not mask a cancellation timeout", async () => {
  const dataDir = migratedStore();
  const controller = new AbortController();
  let sigkillSent = false;
  let postKillLivenessProbes = 0;
  const transientPostKillEperm: typeof process.kill = (pid, signal) => {
    if (signal === 0 && sigkillSent) {
      postKillLivenessProbes += 1;
      if (postKillLivenessProbes === 1) {
        const error = new Error("process group is reaping");
        Object.assign(error, { code: "EPERM" });
        throw error;
      }
    }
    const result = process.kill(pid, signal);
    if (signal === "SIGKILL") sigkillSent = true;
    return result;
  };
  const invocation = withHelper(dataDir, transientPostKillEperm).invoke("describe_market_data", request("call_cancel"), { cancelEvent: controller.signal });
  const pids = await waitForPidFile(join(dataDir, "call_cancel.pids"));

  controller.abort();

  const result = await invocation;
  assert.equal(result.error?.code, "TIMEOUT");
  assert.ok(postKillLivenessProbes >= 2);
  await waitForGone(pids);
});

test("h: malformed child stdout is a safe protocol failure", async () => {
  const result = await withHelper(migratedStore()).invoke("describe_market_data", request("call_crash"));
  assert.equal(result.error?.code, "PROTOCOL_ERROR");
});

test("i: a tampered catalog is rejected during construction", () => {
  const dataDir = migratedStore();
  const probe = JSON.parse(execFileSync("uv", ["run", "python", "-c", "import importlib.resources as r,json,shutil; print(json.dumps({'bin':shutil.which('nan-fung-agent-tools'),'assets_dir':str(r.files('nan_fung.agent_tools'))}))"], { cwd: worktreeRoot, encoding: "utf8" }));
  const assetsDir = mkdtempSync(join(tmpdir(), "facade-assets-"));
  for (const name of ["agent_tool_contracts.v1.json", "agent_tool_request.v1.schema.json", "agent_tool_result.v1.schema.json", "agent_tool_contract_catalog.v1.schema.json"]) copyFileSync(join(probe.assets_dir, name), join(assetsDir, name));
  writeFileSync(join(assetsDir, "agent_tool_contracts.v1.json"), "{}");
  assert.throws(() => new FacadeLauncher({ creDataDir: dataDir, assetsDir }), LauncherConfigError);
});

test("j: all six protocol exit classes have result parity", async () => {
  const launcher = withHelper(migratedStore());
  for (const code of [0, 2, 3, 4, 5, 6]) {
    const result = await launcher.invoke("describe_market_data", request(`call_parity_${code}`));
    if (code === 0) assert.equal(result.status, "ok");
    else assert.notEqual(result.error, null);
  }
  const mismatch = await launcher.invoke("describe_market_data", request("call_parity_1"));
  assert.equal(mismatch.error?.code, "PROTOCOL_ERROR");
});

test("k: read selectors return without writer-construction diagnostics", async () => {
  const launcher = new FacadeLauncher({ creDataDir: migratedStore() });
  for (const [selector, args] of [
    ["describe_market_data", {}],
    ["query_market_data", { capability_id: "uk.bank-rate-current", query_kind: "metrics" }],
    ["get_citation_metadata", { citation_refs: ["missing"] }],
  ] as const) {
    const result = await launcher.invoke(selector, request(`call_${selector}`, args));
    assert.ok(["ok", "error"].includes(result.status));
    assert.doesNotMatch(launcher.lastStderr, /OperationalStore|OperationalRefreshBackend|RefreshBroker/);
  }
});

test("l: descendants are killed even when the direct child exits normally", async () => {
  const dataDir = migratedStore();
  const result = await withHelper(dataDir).invoke("describe_market_data", request("call_parent_exit"));
  assert.equal(result.status, "ok");
  await waitForGone(readFileSync(join(dataDir, "call_parent_exit.pids"), "utf8").trim().split("\n").map(Number));
});

test("m: clean-wheel probe resolves the four packaged regular non-symlink files", () => {
  const root = mkdtempSync(join(tmpdir(), "facade-wheel-"));
  const dist = join(root, "dist");
  const venv = join(root, "venv");
  execFileSync("uv", ["build", "--wheel", "--out-dir", dist], { cwd: worktreeRoot });
  execFileSync("uv", ["venv", "--python", "3.12", venv], { cwd: worktreeRoot });
  const wheel = join(dist, readdirSync(dist).find((name) => name.endsWith(".whl")) ?? "missing.whl");
  execFileSync("uv", ["pip", "install", "--python", join(venv, "bin/python"), wheel], { cwd: worktreeRoot });
  const output = execFileSync(join(venv, "bin/python"), ["-c", "import importlib.resources as r,json,os,stat; d=r.files('nan_fung.agent_tools'); names=['agent_tool_contracts.v1.json','agent_tool_request.v1.schema.json','agent_tool_result.v1.schema.json','agent_tool_contract_catalog.v1.schema.json']; print(json.dumps({n:stat.S_ISREG(os.lstat(d.joinpath(n)).st_mode) and not os.path.islink(d.joinpath(n)) for n in names}))"], { cwd: root, encoding: "utf8" });
  assert.deepEqual(Object.values(JSON.parse(output)), [true, true, true, true]);
});

test("n: EPERM during process-group cleanup becomes a typed protocol failure", async () => {
  // Given: cleanup lacks permission to signal the detached process group
  const deniedKill: typeof process.kill = (_pid, _signal) => {
    const error = new Error("operation not permitted");
    Object.assign(error, { code: "EPERM" });
    throw error;
  };

  // When: a normally exiting child reaches cleanup
  const result = await withHelper(migratedStore(), deniedKill).invoke("describe_market_data", request("call_parent_exit"));

  // Then: cleanup cannot be reported as successful
  assert.equal(result.status, "error");
  assert.equal(result.error?.code, "PROTOCOL_ERROR");
});

test("o: timeout cleanup failure settles and reaps the child without an external kill", async () => {
  // Given: a running process group whose injected containment kill is denied
  const dataDir = migratedStore();
  const deniedKill: typeof process.kill = (_pid, _signal) => {
    const error = new Error("operation not permitted");
    Object.assign(error, { code: "EPERM" });
    throw error;
  };
  const invocation = withHelper(dataDir, deniedKill).invoke("describe_market_data", request("call_timeout"), { timeoutSeconds: 3 });
  const pids = await waitForPidFile(join(dataDir, "call_timeout.pids"));
  const groupLeader = pids[0];
  assert.ok(groupLeader !== undefined);

  // When: timeout and failed cleanup happen together
  const result = await Promise.race([
    invocation,
    new Promise<"TIMEOUT">((resolvePromise) => setTimeout(() => resolvePromise("TIMEOUT"), 4_000)),
  ]);

  // Then: the more severe cleanup failure settles without an external kill
  assert.notEqual(result, "TIMEOUT");
  if (result === "TIMEOUT") assert.fail("invocation did not settle after cleanup failed");
  assert.equal(result.status, "error");
  assert.equal(result.error?.code, "PROTOCOL_ERROR");
  await waitForGone(pids);
});

test("p: process group observable after the SIGKILL deadline is a typed cleanup failure", async () => {
  // Given: signals reach the real group but liveness probes keep observing it
  const persistentKill: typeof process.kill = (pid, signal) => {
    if (signal === 0) return true;
    return process.kill(pid, signal);
  };

  // When: cleanup exhausts both termination deadlines
  const result = await Promise.race([
    withHelper(migratedStore(), persistentKill).invoke("describe_market_data", request("call_timeout"), { timeoutSeconds: 0.01 }),
    new Promise<"TIMEOUT">((resolvePromise) => setTimeout(() => resolvePromise("TIMEOUT"), 3_000)),
  ]);

  // Then: persistent descendants are surfaced as a bounded protocol failure
  assert.notEqual(result, "TIMEOUT");
  if (result === "TIMEOUT") assert.fail("persistent-group cleanup did not settle");
  assert.equal(result.status, "error");
  assert.equal(result.error?.code, "PROTOCOL_ERROR");
});
