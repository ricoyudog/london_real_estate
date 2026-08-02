import assert from "node:assert/strict";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const runtimeRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const packageJson = JSON.parse(readFileSync(join(runtimeRoot, "package.json"), "utf8"));
const packageLock = JSON.parse(readFileSync(join(runtimeRoot, "package-lock.json"), "utf8"));

test("Pi minimatch resolves the audited brace-expansion release", () => {
  const fixedVersion = "5.0.9";
  const nestedPath = join(
    runtimeRoot,
    "node_modules",
    "@earendil-works",
    "pi-coding-agent",
    "node_modules",
    "brace-expansion",
  );
  const nestedBalancedPath = join(
    runtimeRoot,
    "node_modules",
    "@earendil-works",
    "pi-coding-agent",
    "node_modules",
    "balanced-match",
  );
  const fixedPath = join(runtimeRoot, "node_modules", "brace-expansion");
  const piMinimatch = createRequire(
    join(runtimeRoot, "node_modules", "@earendil-works", "pi-coding-agent", "node_modules", "minimatch", "package.json"),
  );
  const resolvedRelative = relative(realpathSync(fixedPath), realpathSync(piMinimatch.resolve("brace-expansion")));
  const installed = JSON.parse(
    readFileSync(join(fixedPath, "package.json"), "utf8"),
  );

  assert.equal(packageJson.dependencies["brace-expansion"], fixedVersion);
  assert.equal(packageJson.overrides["brace-expansion"], fixedVersion);
  assert.equal(
    packageLock.packages["node_modules/@earendil-works/pi-coding-agent/node_modules/brace-expansion"].version,
    fixedVersion,
  );
  assert.equal(installed.version, fixedVersion);
  assert.equal(resolvedRelative === ".." || resolvedRelative.startsWith(`..${sep}`), false);
  assert.equal(existsSync(nestedPath), false);
  assert.equal(existsSync(nestedBalancedPath), false);
});
