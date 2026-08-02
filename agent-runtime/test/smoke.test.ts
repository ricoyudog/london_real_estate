import assert from "node:assert/strict";
import process from "node:process";
import test from "node:test";

test("node version meets engine requirement", () => {
  const [major = 0, minor = 0] = process.versions.node.split(".").map(Number);

  assert.ok(
    major > 22 || (major === 22 && minor >= 19),
    `Node ${process.versions.node} does not meet >=22.19.0`,
  );
});
