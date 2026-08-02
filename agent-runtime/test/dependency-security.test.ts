import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
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
  const installed = JSON.parse(
    readFileSync(join(runtimeRoot, "node_modules", "brace-expansion", "package.json"), "utf8"),
  );

  assert.equal(packageJson.dependencies["brace-expansion"], fixedVersion);
  assert.equal(packageJson.overrides["brace-expansion"], fixedVersion);
  assert.equal(
    packageLock.packages["node_modules/@earendil-works/pi-coding-agent/node_modules/brace-expansion"].version,
    fixedVersion,
  );
  assert.equal(installed.version, fixedVersion);
  assert.equal(existsSync(nestedPath), false);
});
