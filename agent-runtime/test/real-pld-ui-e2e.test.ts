import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";
import test from "node:test";

import { chromium, expect, type Page } from "@playwright/test";

import { createApp, type App } from "../src/app.ts";

const question = "How many planning applications were decided in City of London in July 2026? Cite the source.";
const skipReason = "real PLD UI e2e requires RUN_REAL_MODEL_SMOKE=1, PI_MODEL, PI_BASE_URL, and PI_API_KEY";
const enabled = process.env.RUN_REAL_MODEL_SMOKE === "1"
  && process.env.PI_MODEL !== undefined && process.env.PI_MODEL !== ""
  && process.env.PI_BASE_URL !== undefined && process.env.PI_BASE_URL !== ""
  && process.env.PI_API_KEY !== undefined && process.env.PI_API_KEY !== "";
const worktreeRoot = resolve(import.meta.dirname, "../..");

test("real provider browser renders planning complete and unsupported office coverage unavailable", { skip: enabled ? false : skipReason, timeout: 180_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "real-pld-ui-"));
  const dataDir = join(root, "data");
  let app: App | undefined;
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
  try {
    execFileSync("uv", ["run", "python", "agent-runtime/test/helpers/seed_pld_activity.py", dataDir], { cwd: worktreeRoot });
    app = await createApp({
      ctx: { principal: "real-pld-ui", capability_scope_id: "real-pld-ui", allowed_access_classes: ["open"], allowed_capability_ids: ["uk.bank-rate-current", "london-planning-activity"], allowed_refresh_profiles: ["bank-rate-latest", "planning-activity-monthly"] },
      creDataDir: dataDir,
      deployment: { mode: "production", fixture_label: "Real provider PLD fixture" },
      trace: () => undefined,
    });
    const url = await listen(app);
    browser = await chromium.launch();
    const page = await browser.newPage();
    await page.goto(url);
    await expect(page.locator("#connection-label")).toHaveText("Secure session active");
    await ask(page, question);
    await expect(page.locator("#brief-status")).toHaveText("Complete", { timeout: 90_000 });
    await expect(page.locator("#artifact-content")).toContainText("2");
    await expect(page.locator("#artifact-content")).toContainText("Lineage");
    await expect(page.locator("#artifact-content")).toContainText("all use classes");
    await expect(page.locator("#source-list a")).toHaveAttribute("href", /^https:\/\/files\.planning\.data\.gov\.uk\//);
    for (const prompt of ["What is the latest West End vacancy rate?", "What is the City of London project supply pipeline?"]) {
      await ask(page, prompt);
      await expect(page.locator("#brief-status")).toHaveText("Unavailable", { timeout: 90_000 });
      await expect(page.locator("#artifact-content .fact-value")).toHaveCount(0);
    }
  } finally {
    await browser?.close();
    await app?.close();
    rmSync(root, { recursive: true, force: true });
    rmSync(resolve(worktreeRoot, ".playwright-mcp"), { recursive: true, force: true });
  }
});

async function listen(app: App): Promise<string> {
  await new Promise<void>((resolveListen, rejectListen) => {
    app.server.once("error", rejectListen);
    app.server.listen(0, "127.0.0.1", () => {
      app.server.off("error", rejectListen);
      resolveListen();
    });
  });
  const address = app.server.address();
  assert.ok(typeof address === "object" && address !== null);
  return `http://127.0.0.1:${address.port}`;
}

async function ask(page: Page, message: string): Promise<void> {
  await page.locator("#chat-input").fill(message);
  await page.locator("#send-button").click();
  await expect(page.locator("#chat-input")).toBeEnabled({ timeout: 90_000 });
}
