import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { lstatSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import process from "node:process";
import { Writable } from "node:stream";

import { Ajv2020, type ValidateFunction } from "ajv/dist/2020.js";
import draft2020MetaSchema from "ajv/dist/refs/json-schema-2020-12/schema.json" with { type: "json" };

// allow: SIZE_OK — the launcher is one indivisible child-process security boundary.
export const MAX_STDIN_BYTES = 65_536;
const MAX_STDOUT_BYTES = 262_144;
const MAX_STDERR_BYTES = 65_536;
const DEFAULT_TIMEOUT_SECONDS = 10;
const ASSET_NAMES = [
  "agent_tool_contracts.v1.json",
  "agent_tool_request.v1.schema.json",
  "agent_tool_result.v1.schema.json",
  "agent_tool_contract_catalog.v1.schema.json",
] as const;

type JsonObject = Readonly<Record<string, unknown>>;
type ToolError = {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
};
export type ToolResult = {
  readonly schema_version: "agent_tool_result.v1";
  readonly request_id: string | null;
  readonly status: "ok" | "partial" | "error";
  readonly data: JsonObject | null;
  readonly warnings: readonly string[];
  readonly error: ToolError | null;
};
type LauncherOptions = {
  readonly creDataDir: string;
  readonly instanceId?: string;
  readonly binaryPath?: string;
  readonly assetsDir?: string;
};
type InvokeOptions = {
  readonly timeoutSeconds?: number;
  readonly cancelEvent?: AbortSignal;
};
type Contract = {
  readonly selector: string;
  readonly refresh_request_id: "required" | "forbidden";
  readonly arguments_schema: JsonObject;
  readonly success_data_schema: JsonObject;
};
type CompiledContract = Contract & {
  readonly validateArguments: ValidateFunction;
  readonly validateSuccess: ValidateFunction;
};
export class LauncherConfigError extends Error {
  readonly name = "LauncherConfigError";
}

class ProtocolBoundaryError extends Error {
  readonly name = "ProtocolBoundaryError";
  readonly code: "PROTOCOL_ERROR" | "RESULT_TOO_LARGE" | "TIMEOUT";
  constructor(code: "PROTOCOL_ERROR" | "RESULT_TOO_LARGE" | "TIMEOUT") {
    super(code);
    this.code = code;
  }
}

export class FacadeLauncher {
  readonly #binaryPath: string;
  readonly #assetsDir: string;
  readonly #privateCwd = mkdtempSync(join(tmpdir(), "nan-fung-facade-"));
  readonly #environment: NodeJS.ProcessEnv;
  readonly #handleKey = randomBytes(32);
  readonly #validateRequest: ValidateFunction;
  readonly #validateResult: ValidateFunction;
  readonly #contracts: ReadonlyMap<string, CompiledContract>;
  lastStderr = "";

  constructor(options: LauncherOptions) {
    if (!isAbsolute(options.creDataDir)) throw new LauncherConfigError("CRE_DATA_DIR must be absolute");
    const resolved = resolveAssets(options.binaryPath, options.assetsDir);
    this.#binaryPath = resolved.binaryPath;
    this.#assetsDir = resolved.assetsDir;
    this.#environment = {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      HOME: process.env.HOME ?? tmpdir(),
      CRE_DATA_DIR: resolve(options.creDataDir),
      CRE_ENVIRONMENT: "development",
      CRE_INSTANCE_ID: options.instanceId ?? "pi-agent-runtime-phase-2",
    };

    const ajv = new Ajv2020({ allErrors: true, strict: true, allowUnionTypes: true });
    ajv.removeSchema("https://json-schema.org/draft/2020-12/schema");
    ajv.addMetaSchema(draft2020MetaSchema);
    ajv.addFormat("date", true);
    ajv.addFormat("date-time", true);
    const catalog = readJson(join(this.#assetsDir, ASSET_NAMES[0]));
    const catalogSchema = readJson(join(this.#assetsDir, ASSET_NAMES[3]));
    const validateCatalog = ajv.compile(catalogSchema);
    if (!validateCatalog(catalog)) throw new LauncherConfigError("tool contract catalog is invalid");
    const requestSchema = readJson(join(this.#assetsDir, ASSET_NAMES[1]));
    const definitions = requestSchema.$defs;
    if (isObject(definitions) && isObject(definitions.non_empty_strings) && Array.isArray(definitions.non_empty_strings.allOf)) {
      const minimumItems = definitions.non_empty_strings.allOf[1];
      if (isObject(minimumItems)) minimumItems.type = "array";
    }
    this.#validateRequest = ajv.compile(requestSchema);
    this.#validateResult = ajv.compile(readJson(join(this.#assetsDir, ASSET_NAMES[2])));
    this.#contracts = compileContracts(ajv, catalog);
  }

  async invoke(toolName: string, request: unknown, options: InvokeOptions = {}): Promise<ToolResult> {
    const requestId = requestIdOf(request);
    const contract = this.#contracts.get(toolName);
    if (contract === undefined) return failure(requestId, "INVALID_ARGUMENT");
    if (!this.#validateRequest(request) || !isObject(request)) return failure(requestId, "INVALID_ARGUMENT");
    if (!contract.validateArguments(request.arguments)) return failure(requestId, "INVALID_ARGUMENT");
    if (!refreshPolicyMatches(contract, request.host_context)) return failure(requestId, "INVALID_ARGUMENT");
    const timeoutSeconds = options.timeoutSeconds ?? DEFAULT_TIMEOUT_SECONDS;
    if (!(timeoutSeconds > 0)) return failure(requestId, "INVALID_ARGUMENT");
    let payload: Buffer;
    try {
      payload = Buffer.from(JSON.stringify(request));
    } catch (error) {
      if (error instanceof TypeError) return failure(requestId, "INVALID_ARGUMENT");
      throw error;
    }
    if (payload.byteLength > MAX_STDIN_BYTES) return failure(requestId, "INVALID_ARGUMENT");

    try {
      const execution = await this.#runChild(toolName, payload, timeoutSeconds, options.cancelEvent);
      this.lastStderr = execution.stderr;
      const parsed: unknown = JSON.parse(execution.stdout);
      if (!this.#validateResult(parsed) || !isToolResult(parsed)) throw new ProtocolBoundaryError("PROTOCOL_ERROR");
      if (parsed.request_id !== requestId) throw new ProtocolBoundaryError("PROTOCOL_ERROR");
      if ((parsed.status === "ok" || parsed.status === "partial") && !contract.validateSuccess(parsed.data)) {
        throw new ProtocolBoundaryError("PROTOCOL_ERROR");
      }
      if (execution.exitCode !== exitCodeFor(parsed)) throw new ProtocolBoundaryError("PROTOCOL_ERROR");
      return parsed;
    } catch (error) {
      if (error instanceof ProtocolBoundaryError) return failure(requestId, error.code);
      if (error instanceof SyntaxError) return failure(requestId, "PROTOCOL_ERROR");
      if (error instanceof Error) return failure(requestId, "PROTOCOL_ERROR");
      throw error;
    }
  }

  async #runChild(toolName: string, payload: Buffer, timeoutSeconds: number, cancelEvent?: AbortSignal) {
    const child = spawn(this.#binaryPath, [toolName], {
      shell: false,
      detached: true,
      cwd: this.#privateCwd,
      env: this.#environment,
      stdio: ["pipe", "pipe", "pipe", "pipe"],
    });
    const handlePipe = child.stdio[3];
    if (!(handlePipe instanceof Writable)) throw new ProtocolBoundaryError("PROTOCOL_ERROR");
    handlePipe.end(this.#handleKey);
    child.stdin.end(payload);
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let boundaryError: ProtocolBoundaryError | undefined;
    let cleanup: Promise<void> | undefined;
    const stop = (error: ProtocolBoundaryError) => {
      boundaryError ??= error;
      cleanup ??= terminateGroup(child);
    };
    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBytes += chunk.byteLength;
      if (stdoutBytes > MAX_STDOUT_BYTES) stop(new ProtocolBoundaryError("RESULT_TOO_LARGE"));
      else stdout.push(chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => {
      if (stderrBytes < MAX_STDERR_BYTES) {
        const bounded = chunk.subarray(0, MAX_STDERR_BYTES - stderrBytes);
        stderr.push(bounded);
        stderrBytes += bounded.byteLength;
      }
    });
    const timeout = setTimeout(() => stop(new ProtocolBoundaryError("TIMEOUT")), timeoutSeconds * 1_000);
    const cancel = () => stop(new ProtocolBoundaryError("TIMEOUT"));
    cancelEvent?.addEventListener("abort", cancel, { once: true });
    if (cancelEvent?.aborted) cancel();
    const exitCode = await new Promise<number | null>((resolveExit, rejectExit) => {
      child.once("error", rejectExit);
      child.once("exit", resolveExit);
    });
    clearTimeout(timeout);
    cancelEvent?.removeEventListener("abort", cancel);
    cleanup ??= terminateGroup(child);
    await cleanup;
    if (boundaryError !== undefined) throw boundaryError;
    if (exitCode === null) throw new ProtocolBoundaryError("PROTOCOL_ERROR");
    return {
      exitCode,
      stdout: Buffer.concat(stdout).toString("utf8"),
      stderr: Buffer.concat(stderr).toString("utf8"),
    };
  }
}

function resolveAssets(binaryOverride?: string, assetsOverride?: string) {
  const probe = spawnSync("uv", ["run", "python", "-c", "import shutil, importlib.resources as r, json; d=str(r.files('nan_fung.agent_tools')); print(json.dumps({'bin': shutil.which('nan-fung-agent-tools'), 'assets_dir': d}))"], {
    cwd: resolve(import.meta.dirname, "../.."), encoding: "utf8",
  });
  if (probe.status !== 0) throw new LauncherConfigError("agent-tool resolution probe failed");
  const value = readJsonText(probe.stdout);
  const binaryPath = binaryOverride ?? process.env.NAN_FUNG_AGENT_TOOLS_BIN ?? value.bin;
  const assetsDir = assetsOverride ?? value.assets_dir;
  if (typeof binaryPath !== "string" || !isAbsolute(binaryPath)) throw new LauncherConfigError("agent-tool binary must be absolute");
  if (typeof assetsDir !== "string" || !isAbsolute(assetsDir)) throw new LauncherConfigError("agent-tool assets must be absolute");
  for (const name of ASSET_NAMES) {
    try {
      const stat = lstatSync(join(assetsDir, name));
      if (!stat.isFile() || stat.isSymbolicLink()) throw new LauncherConfigError(`unsafe agent-tool asset: ${name}`);
    } catch (error) {
      if (error instanceof LauncherConfigError) throw error;
      if (error instanceof Error) throw new LauncherConfigError(`agent-tool asset is unavailable: ${name}`, { cause: error });
      throw error;
    }
  }
  return { binaryPath, assetsDir };
}

function compileContracts(ajv: Ajv2020, catalog: unknown): ReadonlyMap<string, CompiledContract> {
  if (!isObject(catalog) || !Array.isArray(catalog.contracts)) throw new LauncherConfigError("catalog contracts are unavailable");
  const contracts = new Map<string, CompiledContract>();
  for (const candidate of catalog.contracts) {
    if (!isContract(candidate) || contracts.has(candidate.selector)) throw new LauncherConfigError("catalog contract is invalid");
    contracts.set(candidate.selector, {
      ...candidate,
      validateArguments: ajv.compile(candidate.arguments_schema),
      validateSuccess: ajv.compile(candidate.success_data_schema),
    });
  }
  return contracts;
}

async function terminateGroup(child: ChildProcess): Promise<void> {
  const pid = child.pid;
  if (pid === undefined) return;
  try { process.kill(-pid, "SIGTERM"); } catch (error) { if (!isMissingProcess(error)) throw error; return; }
  const deadline = performance.now() + 1_000;
  while (performance.now() < deadline) {
    try { process.kill(-pid, 0); } catch (error) { if (isMissingProcess(error)) return; throw error; }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));
  }
  try { process.kill(-pid, "SIGKILL"); } catch (error) { if (!isMissingProcess(error)) throw error; }
  const killDeadline = performance.now() + 1_000;
  while (performance.now() < killDeadline) {
    try { process.kill(-pid, 0); } catch (error) { if (isMissingProcess(error)) return; throw error; }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));
  }
}

function readJson(path: string): JsonObject { return readJsonText(readFileSync(path, "utf8")); }
function readJsonText(text: string): Readonly<Record<string, unknown>> {
  try {
    const value: unknown = JSON.parse(text);
    if (!isObject(value)) throw new LauncherConfigError("JSON asset root must be an object");
    return value;
  } catch (error) {
    if (error instanceof LauncherConfigError) throw error;
    if (error instanceof SyntaxError) throw new LauncherConfigError("JSON asset is invalid", { cause: error });
    throw error;
  }
}
function isObject(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function requestIdOf(value: unknown): string | null { return isObject(value) && typeof value.request_id === "string" ? value.request_id : null; }
function isContract(value: unknown): value is Contract {
  return isObject(value) && typeof value.selector === "string" && (value.refresh_request_id === "required" || value.refresh_request_id === "forbidden") && isObject(value.arguments_schema) && isObject(value.success_data_schema);
}
function refreshPolicyMatches(contract: Contract, hostContext: unknown): boolean {
  if (!isObject(hostContext)) return false;
  const present = Object.hasOwn(hostContext, "refresh_request_id");
  return contract.refresh_request_id === "required" ? present : !present;
}
function isToolResult(value: unknown): value is ToolResult {
  return isObject(value) && value.schema_version === "agent_tool_result.v1" && (value.status === "ok" || value.status === "partial" || value.status === "error") && Array.isArray(value.warnings);
}
function exitCodeFor(result: ToolResult): number {
  if (result.status !== "error") return 0;
  const code = result.error?.code;
  if (code === "INVALID_ARGUMENT" || code === "INVALID_CURSOR") return 2;
  if (code === "ACCESS_DENIED" || code === "CAPABILITY_BLOCKED" || code === "POLICY_DENIED") return 3;
  if (code === "RETRYABLE_UNAVAILABLE" || code === "TIMEOUT") return 4;
  if (code === "INTERNAL_ERROR") return 5;
  return 6;
}
function failure(requestId: string | null, code: string): ToolResult {
  const details: Readonly<Record<string, readonly [string, boolean]>> = {
    INVALID_ARGUMENT: ["The request arguments are invalid.", false],
    RESULT_TOO_LARGE: ["The tool result exceeds the response limit.", false],
    TIMEOUT: ["The tool call timed out.", true],
    PROTOCOL_ERROR: ["The tool protocol was violated.", false],
  };
  const selected = details[code] ?? ["The tool protocol was violated.", false];
  return { schema_version: "agent_tool_result.v1", request_id: requestId, status: "error", data: null, warnings: [], error: { code, message: selected[0], retryable: selected[1] } };
}
function isMissingProcess(error: unknown): boolean { return error instanceof Error && "code" in error && (error.code === "ESRCH" || error.code === "EPERM"); }
