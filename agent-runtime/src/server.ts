import { resolve } from "node:path";
import process from "node:process";

import { createApp } from "./app.ts";

const creDataDir = resolve(requiredEnv("CRE_DATA_DIR"));
const port = portFrom(process.env.PORT);
const host = process.env.HOST?.trim() || "127.0.0.1";

requiredEnv("PI_MODEL");
requiredEnv("PI_BASE_URL");
requiredEnv("PI_API_KEY");

const app = await createApp({
  ctx: {
    principal: "dashboard-user",
    capability_scope_id: "dashboard-bootstrap",
    allowed_access_classes: ["open"],
    allowed_capability_ids: ["uk.bank-rate-current"],
    allowed_refresh_profiles: ["bank-rate-latest"],
  },
  creDataDir,
});

await new Promise<void>((resolveListen, rejectListen) => {
  app.server.once("error", rejectListen);
  app.server.listen(port, host, () => {
    app.server.off("error", rejectListen);
    resolveListen();
  });
});
console.log(`London Market Desk listening at http://${host}:${port}`);

let closing = false;
const close = (signal: string) => {
  if (closing) return;
  closing = true;
  void app.close().then(
    () => process.exit(0),
    () => process.exit(1),
  );
  console.log(`Received ${signal}; closing London Market Desk.`);
};
process.once("SIGINT", () => close("SIGINT"));
process.once("SIGTERM", () => close("SIGTERM"));

function requiredEnv(name: "CRE_DATA_DIR" | "PI_MODEL" | "PI_BASE_URL" | "PI_API_KEY"): string {
  const value = process.env[name]?.trim();
  if (value === undefined || value === "") throw new Error(`${name} required`);
  return value;
}

function portFrom(value: string | undefined): number {
  if (value === undefined || value === "") return 8787;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error("PORT must be an integer from 1 to 65535");
  return port;
}
