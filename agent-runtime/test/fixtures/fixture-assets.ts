import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const assetNames = [
  "agent_tool_contracts.v1.json",
  "agent_tool_request.v1.schema.json",
  "agent_tool_result.v1.schema.json",
  "agent_tool_contract_catalog.v1.schema.json",
] as const;

export function fixtureAssets(worktreeRoot: string): string {
  const source = resolve(worktreeRoot, "src/nan_fung/agent_tools");
  const directory = mkdtempSync(join(tmpdir(), "pi-fixture-assets-"));
  for (const name of assetNames) copyFileSync(join(source, name), join(directory, name));
  const path = join(directory, assetNames[0]);
  const catalog: unknown = JSON.parse(readFileSync(path, "utf8"));
  assert.ok(isRecord(catalog) && Array.isArray(catalog["contracts"]));
  const citation = catalog["contracts"].find((entry) => isRecord(entry) && entry["selector"] === "get_citation_metadata");
  assert.ok(isRecord(citation));
  const schema = citation["success_data_schema"];
  assert.ok(isRecord(schema));
  allowNestedLocator(schema);
  writeFileSync(path, `${JSON.stringify(catalog, null, 2)}\n`);
  return directory;
}

function allowNestedLocator(value: unknown): void {
  if (Array.isArray(value)) {
    for (const item of value) allowNestedLocator(item);
    return;
  }
  if (!isRecord(value)) return;
  if (value["maxProperties"] === 64 && isRecord(value["additionalProperties"])) {
    const variants = value["additionalProperties"]["oneOf"];
    if (Array.isArray(variants)) variants.push({ type: "object" });
  }
  for (const item of Object.values(value)) allowNestedLocator(item);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
