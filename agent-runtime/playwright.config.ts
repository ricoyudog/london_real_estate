import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./test/browser",
  testMatch: "*.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  reporter: [["line"]],
  use: {
    baseURL: "http://127.0.0.1:8799",
    browserName: "chromium",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "node --experimental-strip-types test/browser/server.ts",
    url: "http://127.0.0.1:8799/",
    reuseExistingServer: false,
    timeout: 90_000,
  },
});
