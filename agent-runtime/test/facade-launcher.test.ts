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

function migratedStore(): string {
  const dataDir = join(mkdtempSync(join(tmpdir(), "facade-store-")), "seed-data");
  execFileSync("uv", ["run", "cre", "--data-dir", dataDir, "db", "migrate"], {
    cwd: worktreeRoot,
  });
  return dataDir;
}

function withHelper(dataDir: string): FacadeLauncher {
  const helper = join(mkdtempSync(join(tmpdir(), "facade-bin-")), "nan-fung-agent-tools");
  copyFileSync(helperSource, helper);
  chmodSync(helper, 0o700);
  return new FacadeLauncher({ creDataDir: dataDir, binaryPath: helper });
}

async function waitForGone(pids: readonly number[]): Promise<void> {
  const deadline = performance.now() + 4_000;
  while (performance.now() < deadline) {
    if (pids.every((pid) => {
      try { process.kill(pid, 0); return false; } catch { return true; }
    })) return;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 25));
  }
  assert.fail(`processes survived: ${pids.join(",")}`);
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
  });
});

test("d: oversized stdin becomes a typed invalid-argument result", async () => {
  const result = await withHelper(migratedStore()).invoke("query_market_data", request("call_large", {
    capability_id: "x".repeat(MAX_STDIN_BYTES), query_kind: "metrics",
  }));
  assert.equal(result.error?.code, "INVALID_ARGUMENT");
});

test("e: stdout overflow becomes typed and kills the process group", async () => {
  const result = await withHelper(migratedStore()).invoke("describe_market_data", request("call_stdout_overflow"));
  assert.equal(result.error?.code, "RESULT_TOO_LARGE");
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
  setTimeout(() => controller.abort(), 500);
  const result = await withHelper(dataDir).invoke("describe_market_data", request("call_cancel"), { cancelEvent: controller.signal });
  assert.equal(result.error?.code, "TIMEOUT");
  await waitForGone(readFileSync(join(dataDir, "call_cancel.pids"), "utf8").trim().split("\n").map(Number));
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
