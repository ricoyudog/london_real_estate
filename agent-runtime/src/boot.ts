import { createHash } from "node:crypto";
import { lstatSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";

import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
  type CreateAgentSessionOptions,
} from "@earendil-works/pi-coding-agent";

import { FacadeLauncher, type FacadeLauncher as FacadeLauncherType } from "./facade-launcher.ts";
import type { TurnContext, SessionContext } from "./runtime.ts";
import { createSessionTools, type SessionToolDependencies } from "./tools.ts";

const MAX_SKILL_BYTES = 64 * 1024;
const activeToolNames = [
  "describe_market_data",
  "query_market_data",
  "get_citation_metadata",
  "request_data_refresh",
  "get_refresh_status",
  "finalize_market_brief",
] as const;
const forbiddenToolNames = ["read", "grep", "find", "ls", "bash", "edit", "write", "approve_refresh"] as const;

export type SkillManifestEntry = {
  readonly path: string;
  readonly sha256: string;
  readonly bytes: number;
};

export type SkillManifest = {
  readonly schema_version: "skills_manifest.v1";
  readonly files: readonly SkillManifestEntry[];
};

export interface BootedRuntime {
  readonly session: unknown;
  readonly tools: ReturnType<typeof createSessionTools>;
  readonly ctx: SessionContext;
  readonly launcher: FacadeLauncherType;
  readonly finalizeBrief: (draft: unknown, turn: TurnContext) => Promise<unknown>;
  readonly getTurnContext: () => TurnContext | undefined;
  readonly setTurnContext: (turn: TurnContext | undefined) => void;
  readonly setTurnPolicies: (policies: Pick<SessionToolDependencies, "onResult" | "preToolCall">) => void;
}

type ModelCollection = {
  readonly getModel: (provider: string, model: string) => NonNullable<CreateAgentSessionOptions["model"]> | undefined;
};

type SessionFactory = (options: CreateAgentSessionOptions) => Promise<{ readonly session: unknown }>;

export type BootOptions = {
  readonly modelsOverride?: unknown;
  readonly sessionOverrides?: Partial<CreateAgentSessionOptions>;
  readonly createSession?: SessionFactory;
  readonly launcher?: FacadeLauncherType;
  readonly finalizeBrief?: (draft: unknown, turn: TurnContext) => Promise<unknown>;
  readonly getTurnContext?: () => TurnContext | undefined;
  readonly onResult?: SessionToolDependencies["onResult"];
  readonly preToolCall?: SessionToolDependencies["preToolCall"];
};

export class BootError extends Error {
  readonly name = "BootError";
}

export async function bootRuntime(ctx: SessionContext, options: BootOptions = {}): Promise<BootedRuntime> {
  const configuredModel = requiredEnv("PI_MODEL");
  const creDataDir = requiredEnv("CRE_DATA_DIR");
  const [provider, modelId] = parseModel(configuredModel);
  const repoRoot = resolve(import.meta.dirname, "../..");
  const manifest = loadManifest(join(repoRoot, "agent-runtime", "skills.manifest.json"));
  const skills = verifySkills(manifest.files.map((entry) => resolve(repoRoot, entry.path)), manifest);
  const systemPrompt = assembleSystemPrompt(skills);
  const cwd = mkdtempSync(join(tmpdir(), "pi-agent-"));
  const agentDir = mkdtempSync(join(tmpdir(), "pi-agent-"));
  const settingsManager = SettingsManager.inMemory();
  const sessionManager = SessionManager.inMemory(cwd);
  const resourceLoader = new DefaultResourceLoader({
    cwd,
    agentDir,
    settingsManager,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPromptOverride: () => systemPrompt,
  });
  await resourceLoader.reload();
  assertResourceLockdown(resourceLoader);

  // why: task 9 injects a faux models collection while retaining the real Pi session path.
  const models = options.modelsOverride ?? await ModelRuntime.create({ modelsPath: null });
  const selectedModel = resolveModel(models, provider, modelId);
  if (models instanceof ModelRuntime && !models.hasConfiguredAuth(provider)) {
    throw new BootError("PI_MODEL is unauthorized");
  }
  const launcher = options.launcher ?? new FacadeLauncher({ creDataDir });
  let activeTurn: TurnContext | undefined;
  let policies: Pick<SessionToolDependencies, "onResult" | "preToolCall"> = {
    ...(options.onResult === undefined ? {} : { onResult: options.onResult }),
    ...(options.preToolCall === undefined ? {} : { preToolCall: options.preToolCall }),
  };
  const getTurnContext = options.getTurnContext ?? (() => activeTurn);
  const setTurnContext = (turn: TurnContext | undefined): void => { activeTurn = turn; };
  const finalizeBrief = options.finalizeBrief ?? notWiredFinalizer;
  const tools = createSessionTools({
    ctx,
    launcher,
    finalizeBrief,
    getTurnContext,
    onResult: (toolName, result, turn) => policies.onResult?.(toolName, result, turn),
    preToolCall: (toolName, args, turn) => policies.preToolCall?.(toolName, args, turn),
  });
  assertExactTools(tools);

  const sessionOptions: CreateAgentSessionOptions = {
    ...options.sessionOverrides,
    settingsManager,
    sessionManager,
    resourceLoader,
    customTools: tools,
    noTools: "all",
    tools: [...activeToolNames],
    cwd,
    agentDir,
    // why: Pi's runtime accepts the Models surface, while task 9 injects a faux Models collection.
    modelRuntime: models as ModelRuntime,
    model: selectedModel,
  };
  const created = await (options.createSession ?? createAgentSession)(sessionOptions);
  assertExactTools(tools, created.session);
  return {
    session: created.session, tools, ctx, launcher, finalizeBrief, getTurnContext, setTurnContext,
    setTurnPolicies: (next) => { policies = next; },
  };
}

export function verifySkills(
  files: readonly string[],
  manifest: SkillManifest,
  discoveredFiles: readonly string[] = [],
): readonly VerifiedSkill[] {
  if (files.length !== manifest.files.length) throw new BootError("skill manifest file count mismatch");
  const expectedPaths = new Set(files.map((path) => resolve(path)));
  for (const discovered of discoveredFiles) {
    if (!expectedPaths.has(resolve(discovered))) throw new BootError("discovered skill is not in manifest");
  }
  return files.map((path, index) => {
    const entry = manifest.files[index];
    if (entry === undefined) throw new BootError("skill manifest file count mismatch");
    return { path, content: verifySkillFile(path, entry) };
  });
}

export function verifySkillFile(path: string, entry: SkillManifestEntry): string {
  let stat: ReturnType<typeof lstatSync>;
  try {
    stat = lstatSync(path);
  } catch (error) {
    if (error instanceof Error) throw new BootError("skill is unavailable", { cause: error });
    throw error;
  }
  if (!stat.isFile() || stat.isSymbolicLink()) throw new BootError("skill must be a regular file");
  if (stat.size > MAX_SKILL_BYTES) throw new BootError("skill exceeds 64 KiB");
  const content = readFileSync(path, "utf8");
  if (createHash("sha256").update(content).digest("hex") !== entry.sha256) throw new BootError("skill hash mismatch");
  return content;
}

export function assertExactTools(tools: readonly { readonly name: string }[], session?: unknown): void {
  const supplied = tools.map((tool) => tool.name).sort();
  const expected = [...activeToolNames].sort();
  if (supplied.length !== expected.length || supplied.some((name, index) => name !== expected[index])) {
    throw new BootError("active tool set drift");
  }
  if (forbiddenToolNames.some((name) => supplied.includes(name))) throw new BootError("forbidden tool registered");
  const sessionNames = activeToolsFrom(session);
  if (sessionNames !== undefined) assertExactTools(sessionNames.map((name) => ({ name })));
}

type VerifiedSkill = { readonly path: string; readonly content: string };

function requiredEnv(name: "PI_MODEL" | "CRE_DATA_DIR"): string {
  const value = process.env[name];
  if (value === undefined || value.trim() === "") throw new BootError(`${name} required`);
  return value;
}

function parseModel(value: string): readonly [string, string] {
  const separator = value.indexOf("/");
  if (separator <= 0 || separator === value.length - 1) throw new BootError("PI_MODEL must be provider/model");
  return [value.slice(0, separator), value.slice(separator + 1)];
}

function loadManifest(path: string): SkillManifest {
  let value: unknown;
  try {
    value = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    if (error instanceof Error) throw new BootError("skills manifest is unavailable", { cause: error });
    throw error;
  }
  if (!isManifest(value)) throw new BootError("skills manifest is invalid");
  return value;
}

function isManifest(value: unknown): value is SkillManifest {
  if (!isRecord(value) || value.schema_version !== "skills_manifest.v1" || !Array.isArray(value.files)) return false;
  return value.files.every((entry) => isRecord(entry) && typeof entry.path === "string" && typeof entry.sha256 === "string" && typeof entry.bytes === "number");
}

function resolveModel(models: unknown, provider: string, modelId: string): NonNullable<CreateAgentSessionOptions["model"]> {
  if (!isModelCollection(models)) throw new BootError("models collection is invalid");
  const model = models.getModel(provider, modelId);
  if (model === undefined) throw new BootError("PI_MODEL is unavailable");
  return model;
}

function isModelCollection(value: unknown): value is ModelCollection {
  return isRecord(value) && typeof value.getModel === "function";
}

function assembleSystemPrompt(skills: readonly VerifiedSkill[]): string {
  return skills.map((skill) => `<!-- ${skill.path} -->\n${skill.content}`).join("\n\n");
}

function assertResourceLockdown(resourceLoader: DefaultResourceLoader): void {
  if (resourceLoader.getExtensions().extensions.length > 0) throw new BootError("extension discovery is enabled");
  if (resourceLoader.getSkills().skills.length > 0) throw new BootError("skill discovery is enabled");
  if (resourceLoader.getPrompts().prompts.length > 0) throw new BootError("prompt template discovery is enabled");
  if (resourceLoader.getThemes().themes.length > 0) throw new BootError("theme discovery is enabled");
  if (resourceLoader.getAgentsFiles().agentsFiles.length > 0) throw new BootError("context file discovery is enabled");
}

function activeToolsFrom(session: unknown): readonly string[] | undefined {
  if (!isRecord(session) || !Array.isArray(session.activeToolNames)) return undefined;
  return session.activeToolNames.filter((name): name is string => typeof name === "string");
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function notWiredFinalizer(_draft: unknown, _turn: TurnContext): Promise<unknown> {
  // wired in task 8
  throw new BootError("finalizeBrief not wired");
}

function notWiredTurnContext(): TurnContext | undefined {
  // wired in task 8
  throw new BootError("finalizeBrief not wired");
}
