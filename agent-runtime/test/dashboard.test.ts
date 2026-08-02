import assert from "node:assert/strict";
import test from "node:test";

import { DashboardService } from "../src/dashboard.ts";
import type { ToolResult } from "../src/facade-launcher.ts";

const ctx = {
  principal: "dashboard",
  capability_scope_id: "bootstrap",
  allowed_access_classes: ["open"],
  allowed_capability_ids: ["uk.bank-rate-current"],
  allowed_refresh_profiles: ["bank-rate-latest"],
} as const;

test("dashboard overview derives its Bank Rate and coverage from scoped Facade results", async () => {
  const launcher = new DashboardLauncher();
  const service = new DashboardService({
    ctx,
    launcher,
    deployment: { mode: "demo", fixture_label: "Deterministic Bank Rate fixture" },
  });

  const overview = await service.overview({ principal: "anonymous", scope_id: "scope_browser" });

  assert.deepEqual(launcher.calls.map((call) => call.toolName), ["describe_market_data", "query_market_data", "get_citation_metadata"]);
  const describeContext = requestContext(launcher.calls[0]?.request);
  const queryContext = requestContext(launcher.calls[1]?.request);
  assert.equal(describeContext.capability_scope_id, "scope_browser");
  assert.deepEqual(queryContext.allowed_capability_ids, ["uk.bank-rate-current"]);
  assert.equal(overview.bank_rate.value, "5.25");
  assert.equal(overview.bank_rate.source?.publisher, "Bank of England");
  assert.deepEqual(overview.deployment, { mode: "demo", fixture_label: "Deterministic Bank Rate fixture" });
  assert.equal(overview.coverage.find((item) => item.capability_id === "london-prime-rent")?.status, "blocked");
  assert.equal(JSON.stringify(overview).includes("h1.secret-citation-handle"), false);
});

class DashboardLauncher {
  readonly calls: { readonly toolName: string; readonly request: unknown }[] = [];

  async invoke(toolName: string, request: unknown): Promise<ToolResult> {
    this.calls.push({ toolName, request });
    switch (toolName) {
      case "describe_market_data": return ok({ capabilities: [
        capability("uk.bank-rate-current", "supported", null, "fresh"),
        capability("london-prime-rent", "blocked", "Product coverage is not approved.", "unknown"),
        capability("london-office-vacancy", "blocked", "Product coverage is not approved.", "unknown"),
        capability("uk-investment-transactions", "blocked", "Transaction coverage is not approved.", "unknown"),
        capability("uk-ranked-market-news", "blocked", "Ranked news coverage is not approved.", "unknown"),
      ] });
      case "query_market_data": return ok({
        records: [{
          numeric: { value: "5.25", unit: "percent", definition: "Official Bank Rate", as_of: "2026-08-02T00:00:00Z", source_date: "2026-08-01", period_label: "1 Aug 2026" },
          retrieval_freshness: "fresh", observation_freshness: "fresh", degraded: false, citation_refs: ["h1.secret-citation-handle"],
        }],
      });
      case "get_citation_metadata": return ok({ citations: [{ publisher: "Bank of England", title: "Bank Rate", public_url: "https://example.test/bank-rate", published_at: "2026-08-01T09:00:00Z" }] });
      default: throw new Error(`unexpected tool: ${toolName}`);
    }
  }
}

function capability(capabilityId: string, status: string, blockedReason: string | null, freshness: string) {
  return {
    capability_id: capabilityId, status, blocked_reason: blockedReason,
    canonical_availability: { retrieval_freshness: freshness, observation_freshness: freshness, degraded: freshness === "fresh" ? false : null },
  };
}

function ok(data: Readonly<Record<string, unknown>>): ToolResult {
  return { schema_version: "agent_tool_result.v1", request_id: null, status: "ok", data, warnings: [], error: null };
}

function requestContext(value: unknown): Readonly<Record<string, unknown>> {
  assert.ok(typeof value === "object" && value !== null && "host_context" in value);
  const context = value.host_context;
  assert.ok(typeof context === "object" && context !== null && !Array.isArray(context));
  return context as Readonly<Record<string, unknown>>;
}
