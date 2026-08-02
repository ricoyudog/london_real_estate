import { AxeBuilder } from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

test("loads a fixture-labelled overview and releases sessions across repeated reloads", async ({ page }) => {
  await gotoReady(page);
  await expect(page.getByTestId("demo-banner")).toContainText("Deterministic Bank Rate fixture");
  await expect(page.getByTestId("runtime-badge")).toHaveText("Pi AgentSession · GLM-5.2");
  await expect(page.locator("#bank-rate-value")).toContainText("5.25");
  await expect(page.getByRole("button", { name: /approve|deny/i })).toHaveCount(0);

  for (let index = 0; index < 12; index += 1) {
    await page.reload();
    await expect(page.locator("#connection-label")).toHaveText("Secure session active");
  }
});

test("fails closed when a browser session cannot be created", async ({ page }) => {
  await page.route("**/v1/sessions", async (route) => {
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "unavailable" }) });
  });
  await page.goto("/");
  await expect(page.locator("#connection-label")).toHaveText("Unable to start a session");
  await expect(page.locator("#chat-input")).toBeDisabled();
  await expect(page.locator("#bank-rate-value")).toHaveText("Unavailable");
});

test("renders only the final artifact with freshness, confidence, lineage and safe sources", async ({ page }) => {
  await gotoReady(page);
  await page.getByRole("button", { name: "Latest Bank Rate" }).click();
  await page.getByRole("button", { name: "Send question" }).click();

  await expect(page.locator("#brief-status")).toHaveText("Complete");
  await expect(page.locator("#artifact-content")).toContainText("5.25 percent");
  await expect(page.locator("#artifact-content")).toContainText("Fact confidence: Medium");
  await expect(page.locator("#artifact-content")).toContainText("Datasource confidence: High");
  await expect(page.locator("#artifact-content")).toContainText("Inference confidence: Low");
  await expect(page.locator("#artifact-content")).toContainText("Lineage");
  await expect(page.locator("#artifact-content")).toContainText("As of");
  const source = page.locator("#source-list a").first();
  await expect(source).toHaveAttribute("href", /^https?:\/\//);
  await expect(page.locator("#transcript")).not.toContainText("4%", { useInnerText: true });
});

test("clears a previous result on safe failure, then supports cancel and retry", async ({ page }) => {
  await gotoReady(page);
  await ask(page, "What is the latest UK Bank Rate?");
  await expect(page.locator("#artifact-content")).toContainText("5.25 percent");

  await ask(page, "[FAIL] force the numeric guard");
  await expect(page.locator("#artifact-content")).toContainText("Numeric output was rejected before it could become a final artifact.");
  await expect(page.locator("#artifact-content")).not.toContainText("5.25 percent");
  await expect(page.locator("#source-drawer")).toBeHidden();

  await page.locator("#chat-input").fill("[SLOW] wait for cancellation");
  await page.locator("#send-button").click();
  await expect(page.locator("#cancel-button")).toBeEnabled();
  await page.locator("#cancel-button").click();
  await expect(page.locator("#turn-status")).toContainText("cancelled");
  await expect(page.locator("#chat-input")).toBeEnabled();

  await ask(page, "What is the latest UK Bank Rate?");
  await expect(page.locator("#artifact-content")).toContainText("5.25 percent");
});

test("keeps unsupported office coverage unavailable without model-generated numbers", async ({ page }) => {
  await gotoReady(page);
  await ask(page, "What is the latest West End vacancy rate?");
  await expect(page.locator("#brief-status")).toHaveText("Unavailable");
  await expect(page.locator("#artifact-content")).toContainText("requested office-market coverage is not available");
  await expect(page.locator("#artifact-content .fact-value")).toHaveCount(0);
});

test("accepts multilingual, multiline and bounded input while blocking double-submit and the next turn after the limit", async ({ page }) => {
  await gotoReady(page);
  await page.locator("#chat-input").fill("   ");
  await page.locator("#send-button").click();
  await expect(page.locator(".message--user")).toHaveCount(0);

  await page.locator("#chat-input").fill("[FAIL] 中文問題\nEnglish continuation");
  await page.locator("#send-button").dblclick();
  await expect(page.locator(".message--user")).toHaveCount(1);
  await expect(page.locator("#chat-input")).toBeEnabled();

  const bounded = `[FAIL] ${"x".repeat(3993)}`;
  expect(bounded.length).toBe(4000);
  await ask(page, bounded);
  for (let index = 2; index < 16; index += 1) await ask(page, `[FAIL] bounded turn ${index}`);
  await ask(page, "[FAIL] one turn beyond the session limit", false);
  await expect(page.locator("#turn-status")).toContainText("could not be sent");
  await expect(page.locator("#chat-input")).toBeEnabled();
});

test("reconnects and replays the final artifact after a transient offline period", async ({ page, context }) => {
  await gotoReady(page);
  await page.locator("#chat-input").fill("[DELAY] What is the latest UK Bank Rate?");
  await page.locator("#send-button").click();
  await expect(page.locator("#cancel-button")).toBeEnabled();
  await context.setOffline(true);
  await page.waitForTimeout(250);
  await context.setOffline(false);
  await expect(page.locator("#artifact-content")).toContainText("5.25 percent", { timeout: 15_000 });
});

test("renders empty and stale overview states honestly", async ({ page }) => {
  await page.route("**/dashboard/overview", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(overviewFixture("unavailable")) });
  });
  await gotoReady(page);
  await expect(page.locator("#bank-rate-value")).toHaveText("Unavailable");
  await page.unroute("**/dashboard/overview");

  await page.route("**/dashboard/overview", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(overviewFixture("stale")) });
  });
  await page.reload();
  await expect(page.locator("#bank-rate-meta")).toContainText("stale · stale · degraded");
  await expect(page.locator("#bank-rate-meta a")).toHaveCount(0);
});

test("has no serious accessibility violations and remains usable at desktop, tablet and mobile widths", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await gotoReady(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
  }
  await page.keyboard.press("Home");
  await page.keyboard.press("Tab");
  await expect(page.locator(".skip-link")).toBeFocused();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical")).toEqual([]);
});

async function gotoReady(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.locator("#connection-label")).toHaveText("Secure session active");
  await expect(page.locator("#overview-status")).toContainText("Canonical snapshot loaded");
}

async function ask(page: Page, message: string, expectTerminal = true): Promise<void> {
  await page.locator("#chat-input").fill(message);
  await page.locator("#send-button").click();
  if (expectTerminal) await expect(page.locator("#chat-input")).toBeEnabled();
}

function overviewFixture(state: "unavailable" | "stale") {
  const available = state === "stale";
  return {
    schema_version: "dashboard_overview.v1",
    deployment: { mode: "demo", fixture_label: "Deterministic Bank Rate fixture" },
    bank_rate: {
      status: available ? "available" : "unavailable",
      value: available ? "5.25" : null,
      unit: available ? "percent" : null,
      definition: available ? "Official Bank Rate" : null,
      as_of: available ? "2026-08-02T00:00:00Z" : null,
      source_date: available ? "2026-08-01" : null,
      period_label: available ? "Current" : null,
      freshness: { retrieval: available ? "stale" : "unknown", observation: available ? "stale" : "unknown", degraded: available ? true : null },
      source: available ? { publisher: "Unsafe source", title: null, public_url: "javascript:alert(1)", published_at: null } : null,
      reason: available ? null : "No canonical Bank Rate record is available.",
    },
    coverage: [],
  };
}
