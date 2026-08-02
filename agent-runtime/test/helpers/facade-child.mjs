#!/usr/bin/env node
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import process from "node:process";

const key = readFileSync(3);
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const requestText = Buffer.concat(chunks).toString("utf8");
const request = JSON.parse(requestText);
const mode = request.request_id;

if (mode === "call_stdout_overflow") {
  process.stdout.write("x".repeat(262_145));
  setInterval(() => {}, 1_000);
} else if (mode === "call_stderr_overflow") {
  process.stderr.write("x".repeat(65_537));
  process.stdout.write(JSON.stringify(ok(request.request_id)));
} else if (mode === "call_timeout" || mode === "call_cancel") {
  const grandchild = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    stdio: "ignore",
  });
  writeFileSync(`${process.env.CRE_DATA_DIR}/${mode}.pids`, `${process.pid}\n${grandchild.pid}\n`);
  process.on("SIGTERM", () => {});
  setInterval(() => {}, 1_000);
} else if (mode === "call_parent_exit") {
  const grandchild = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    stdio: "ignore",
  });
  grandchild.unref();
  writeFileSync(`${process.env.CRE_DATA_DIR}/${mode}.pids`, `${process.pid}\n${grandchild.pid}\n`);
  process.stdout.write(JSON.stringify(ok(request.request_id)));
} else if (mode === "call_crash") {
  process.stdout.write("not-json");
  process.exitCode = 1;
} else if (mode.startsWith("call_parity_")) {
  const code = Number(mode.slice("call_parity_".length));
  process.stdout.write(JSON.stringify(code === 0 ? ok(mode) : failure(mode, code)));
  process.exitCode = code;
} else {
  writeFileSync(`${process.env.CRE_DATA_DIR}/key-observation.json`, JSON.stringify({
    fd3_bytes: key.length,
    key_absent_from_argv: !process.argv.join("\0").includes(key.toString("hex")),
    key_absent_from_env: !JSON.stringify(process.env).includes(key.toString("hex")),
    key_absent_from_stdin: !requestText.includes(key.toString("hex")),
  }));
  process.stdout.write(JSON.stringify(ok(request.request_id)));
}

function ok(requestId) {
  return {
    schema_version: "agent_tool_result.v1",
    request_id: requestId,
    status: "ok",
    data: { capabilities: [] },
    warnings: [],
    error: null,
  };
}

function failure(requestId, code) {
  const details = {
    2: ["INVALID_ARGUMENT", "The request arguments are invalid.", false],
    3: ["ACCESS_DENIED", "Access to this capability is denied.", false],
    4: ["RETRYABLE_UNAVAILABLE", "The requested service is temporarily unavailable.", true],
    5: ["INTERNAL_ERROR", "The tool could not complete safely.", false],
    6: ["PROTOCOL_ERROR", "The tool protocol was violated.", false],
  }[code];
  return {
    schema_version: "agent_tool_result.v1",
    request_id: requestId,
    status: "error",
    data: null,
    warnings: [],
    error: { code: details[0], message: details[1], retryable: details[2] },
  };
}
