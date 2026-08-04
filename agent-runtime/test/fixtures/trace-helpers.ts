import assert from "node:assert/strict";

import type { ToolResult } from "../../src/facade-launcher.ts";

type Call = {
  readonly toolName: string;
  readonly request: unknown;
  readonly result: ToolResult;
};

type BootedWithCalls = {
  readonly launcher: {
    readonly calls: readonly Call[];
  };
};

type CapabilityTraceExpectation = {
  readonly capability_id: string;
  readonly query_kind?: string;
  readonly datasource_id?: string;
};

export function assertCapabilityTrace(booted: BootedWithCalls, expected: CapabilityTraceExpectation): void {
  const query = booted.launcher.calls.filter((call) => call.toolName === "query_market_data").at(-1);
  assert.ok(query !== undefined);
  const argumentsValue = record(query.request)["arguments"];
  const argumentsRecord = record(argumentsValue);
  assert.equal(argumentsRecord["capability_id"], expected.capability_id);
  if (expected.query_kind !== undefined) assert.equal(argumentsRecord["query_kind"], expected.query_kind);

  if (expected.datasource_id !== undefined) {
    const datasourceIds = record(query.result.data)["datasource_ids"];
    assert.ok(Array.isArray(datasourceIds));
    assert.ok(datasourceIds.includes(expected.datasource_id));
  }
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  assert.ok(isRecord(value));
  return value;
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
