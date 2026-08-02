import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";

import {
  BootError,
  assertExactTools,
  bootRuntime,
  verifySkillFile,
  verifySkills,
  type BootOptions,
  type SkillManifest,
} from "../src/boot.ts";
import type { SessionContext } from "../src/runtime.ts";

const repoRoot = resolve(import.meta.dirname, "../..");
const skillsRoot = join(repoRoot, "skills");
const manifest: SkillManifest = {
  schema_version: "skills_manifest.v1",
  files: [
    { path: "skills/track-uk-macro/SKILL.md", sha256: "ba2a0081b3210dbe4b8626dab11714dfa0da5a0e02f010deabebde20e695096f", bytes: 2912 },
    { path: "skills/generate-grounded-market-brief/SKILL.md", sha256: "58719ab8f928cfab36726a054fc25ba02d9abf5e20a5068e9f3b728c4583fd90", bytes: 2250 },
  ],
};
const context: SessionContext = {
  principal: "principal",
  capability_scope_id: "scope",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current"],
  allowed_refresh_profiles: ["bank-rate-latest"],
};
const expectedTools = [
  "describe_market_data",
  "query_market_data",
  "get_citation_metadata",
  "request_data_refresh",
  "get_refresh_status",
  "finalize_market_brief",
] as const;
const macroSkill = readFileSync(join(skillsRoot, "track-uk-macro", "SKILL.md"), "utf8");
const briefSkill = readFileSync(join(skillsRoot, "generate-grounded-market-brief", "SKILL.md"), "utf8");

test("(a) boot requires PI_MODEL", async () => {
  // Given: no configured model.
  const prior = process.env.PI_MODEL;
  delete process.env.PI_MODEL;

  // When: boot starts.
  await assert.rejects(bootRuntime(context), new BootError("PI_MODEL required"));

  // Then: the environment is restored for independent tests.
  restoreEnv("PI_MODEL", prior);
});

test("(b) boot preloads both verified skills into a locked-down resource loader", async () => {
  // Given: a fake session factory and configured boot environment.
  const priorModel = process.env.PI_MODEL;
  const priorDataDir = process.env.CRE_DATA_DIR;
  process.env.PI_MODEL = "faux/model";
  process.env.CRE_DATA_DIR = mkdtempSync(join(tmpdir(), "boot-data-"));
  let captured: Parameters<NonNullable<BootOptions["createSession"]>>[0] | undefined;
  const modelsOverride = { getModel: () => ({ provider: "faux", id: "model" }) };

  // When: boot wires Pi resources.
  await bootRuntime(context, {
    modelsOverride,
    createSession: async (options) => {
      captured = options;
      return { session: { activeToolNames: expectedTools } };
    },
  });

  // Then: discovery is disabled and the prompt contains both complete skill bodies.
  assert.ok(captured);
  assert.ok(captured.resourceLoader);
  assert.equal(captured.modelRuntime, modelsOverride);
  assert.deepEqual(captured.tools, [...expectedTools]);
  assert.equal(captured.noTools, "all");
  for (const flag of ["noExtensions", "noSkills", "noPromptTemplates", "noThemes", "noContextFiles"]) {
    assert.equal(Reflect.get(captured.resourceLoader, flag), true, flag);
  }
  assert.equal(captured.resourceLoader.getSkills().skills.length, 0);
  assert.equal(captured.resourceLoader.getExtensions().extensions.length, 0);
  assert.equal(captured.resourceLoader.getPrompts().prompts.length, 0);
  assert.equal(captured.resourceLoader.getThemes().themes.length, 0);
  const prompt = captured.resourceLoader.getSystemPrompt();
  assert.ok(prompt?.includes(macroSkill));
  assert.ok(prompt?.includes(briefSkill));
  restoreEnv("PI_MODEL", priorModel);
  restoreEnv("CRE_DATA_DIR", priorDataDir);
});

test("(c) skill verification rejects a tampered skill file", () => {
  // Given: a fresh, individually manifest-backed temporary skill file.
  const directory = mkdtempSync(join(tmpdir(), "boot-skill-"));
  const file = join(directory, "SKILL.md");
  writeFileSync(file, "trusted");
  const entry = entryFor(file, "trusted");

  // When / Then: its digest no longer matches the manifest.
  writeFileSync(file, "tampered");
  assert.throws(() => verifySkillFile(file, entry), new BootError("skill hash mismatch"));
  rmSync(directory, { recursive: true, force: true });
});

test("(d) skill verification rejects a symlink", () => {
  // Given: a manifest-backed path replaced by a symlink.
  const directory = mkdtempSync(join(tmpdir(), "boot-skill-"));
  const file = join(directory, "SKILL.md");
  const entry = entryFor(file, "trusted");
  symlinkSync(join(directory, "target"), file);

  // When / Then: the non-regular file is rejected before content reads.
  assert.throws(() => verifySkillFile(file, entry), new BootError("skill must be a regular file"));
  rmSync(directory, { recursive: true, force: true });
});

test("(e) skill verification rejects a skill over 64 KiB", () => {
  // Given: an oversized regular file.
  const directory = mkdtempSync(join(tmpdir(), "boot-skill-"));
  const file = join(directory, "SKILL.md");
  writeFileSync(file, "x".repeat(65_537));

  // When / Then: its size violates the pre-load boundary.
  assert.throws(() => verifySkillFile(file, entryFor(file, "trusted")), new BootError("skill exceeds 64 KiB"));
  rmSync(directory, { recursive: true, force: true });
});

test("(f) skill verification rejects a deleted skill", () => {
  // Given: an absent manifest path.
  const directory = mkdtempSync(join(tmpdir(), "boot-skill-"));
  const file = join(directory, "SKILL.md");

  // When / Then: availability failure stops boot verification.
  assert.throws(() => verifySkillFile(file, entryFor(file, "trusted")), new BootError("skill is unavailable"));
  rmSync(directory, { recursive: true, force: true });
});

test("(g) boot ignores an extra on-disk skill when discovery is disabled", async () => {
  // Given: the two manifest files plus an unrelated third on-disk skill.
  const extra = mkdtempSync(join(skillsRoot, "boot-extra-"));
  writeFileSync(join(extra, "SKILL.md"), "untrusted");
  const priorModel = process.env.PI_MODEL;
  const priorDataDir = process.env.CRE_DATA_DIR;
  process.env.PI_MODEL = "faux/model";
  process.env.CRE_DATA_DIR = mkdtempSync(join(tmpdir(), "boot-data-"));

  // When: boot verifies only the manifest's two listed files.
  await bootRuntime(context, fakeBootOptions());

  // Then: the unrelated skill does not affect the locked-down session.
  restoreEnv("PI_MODEL", priorModel);
  restoreEnv("CRE_DATA_DIR", priorDataDir);
  rmSync(extra, { recursive: true, force: true });
});

test("(h) discovered skills outside the manifest fail verification", () => {
  // Given: a valid manifest and a discovered third Skill path.
  const extra = mkdtempSync(join(skillsRoot, "boot-extra-"));
  const extraSkill = join(extra, "SKILL.md");
  writeFileSync(extraSkill, "untrusted");
  const resolvedManifest = resolveManifest(manifest);
  const verifiedPaths = resolvedManifest.files.map((entry) => entry.path);

  // When / Then: discovery would be a security boundary violation.
  assert.throws(
    () => verifySkills(verifiedPaths, resolvedManifest, [extraSkill]),
    new BootError("discovered skill is not in manifest"),
  );
  rmSync(extra, { recursive: true, force: true });
});

test("(i) exact tool assertion rejects additions and renames", () => {
  // Given: the decision-defined tool set.
  const valid = expectedTools.map((name) => ({ name }));

  // When / Then: additions and substitutions are rejected.
  assert.doesNotThrow(() => assertExactTools(valid));
  assert.throws(() => assertExactTools([...valid, { name: "seventh" }]), BootError);
  assert.throws(() => assertExactTools([...valid.slice(0, 5), { name: "renamed" }]), BootError);
});

test("(j) exact tool assertion excludes approve_refresh", () => {
  // Given: all permitted tools and the host-only approval tool.
  const tools = [...expectedTools.map((name) => ({ name })), { name: "approve_refresh" }];

  // When / Then: host-only approval cannot reach the model.
  assert.throws(() => assertExactTools(tools), BootError);
});

test("(k) exact tool assertion excludes Pi built-ins", () => {
  // Given: all permitted tools plus each filesystem or shell builtin.
  const valid = expectedTools.map((name) => ({ name }));

  // When / Then: every implicit Pi builtin is rejected.
  for (const forbidden of ["read", "grep", "find", "ls", "bash", "edit", "write"]) {
    assert.throws(() => assertExactTools([...valid, { name: forbidden }]), BootError);
  }
});

function fakeBootOptions(): BootOptions {
  return {
    modelsOverride: { getModel: () => ({ provider: "faux", id: "model" }) },
    createSession: async () => ({ session: { activeToolNames: expectedTools } }),
  };
}

function entryFor(path: string, content: string): SkillManifest["files"][number] {
  return { path, sha256: createHash("sha256").update(content).digest("hex"), bytes: Buffer.byteLength(content) };
}

function resolveManifest(value: SkillManifest): SkillManifest {
  return {
    ...value,
    files: value.files.map((entry) => ({ ...entry, path: join(repoRoot, entry.path) })),
  };
}

function restoreEnv(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}
