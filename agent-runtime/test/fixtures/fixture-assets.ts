import { copyFileSync, mkdtempSync } from "node:fs";
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
  return directory;
}
