import { lstatSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

export type StaticAsset = { readonly content: string; readonly contentType: string };
export type StaticAssets = ReadonlyMap<string, StaticAsset>;

const assetFiles = [
  ["/", "index.html", "text/html; charset=utf-8"],
  ["/app.js", "app.js", "text/javascript; charset=utf-8"],
  ["/styles.css", "styles.css", "text/css; charset=utf-8"],
] as const;

export function loadDashboardAssets(directory = resolve(import.meta.dirname, "../public")): StaticAssets {
  return new Map(assetFiles.map(([route, name, contentType]) => {
    const path = join(directory, name);
    const stat = lstatSync(path);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`dashboard asset is unavailable: ${name}`);
    return [route, { content: readFileSync(path, "utf8"), contentType }] as const;
  }));
}
