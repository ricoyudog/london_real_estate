import { existsSync, lstatSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const runtimeRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const piRoot = join(runtimeRoot, "node_modules", "@earendil-works", "pi-coding-agent");
const nestedPackage = join(piRoot, "node_modules", "brace-expansion");
const nestedDependency = join(piRoot, "node_modules", "balanced-match");
const fixedPackage = join(runtimeRoot, "node_modules", "brace-expansion");
const shrinkwrapPath = join(piRoot, "npm-shrinkwrap.json");
const fixedVersion = "5.0.9";

function packageVersion(path) {
  return JSON.parse(readFileSync(join(path, "package.json"), "utf8")).version;
}

if (packageVersion(fixedPackage) !== fixedVersion) {
  throw new Error(`expected top-level brace-expansion ${fixedVersion}`);
}

for (const [path, expectedVersion] of [[nestedPackage, "5.0.7"], [nestedDependency, "4.0.4"]]) {
  if (!existsSync(path)) continue;
  const stat = lstatSync(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("unexpected nested dependency path");
  if (packageVersion(path) !== expectedVersion) throw new Error(`unexpected nested dependency ${path}`);
}

const shrinkwrap = JSON.parse(readFileSync(shrinkwrapPath, "utf8"));
const packages = shrinkwrap.packages;
if (packages === null || typeof packages !== "object") throw new Error("invalid Pi shrinkwrap packages");
const braceEntry = packages["node_modules/brace-expansion"];
const balancedEntry = packages["node_modules/balanced-match"];
if (braceEntry !== undefined && braceEntry.version !== "5.0.7") {
  throw new Error("unexpected Pi brace-expansion shrinkwrap entry");
}
if (balancedEntry !== undefined && balancedEntry.version !== "4.0.4") {
  throw new Error("unexpected Pi balanced-match shrinkwrap entry");
}

if (existsSync(nestedPackage)) rmSync(nestedPackage, { recursive: true });
if (existsSync(nestedDependency)) rmSync(nestedDependency, { recursive: true });
delete packages["node_modules/brace-expansion"];
delete packages["node_modules/balanced-match"];
writeFileSync(shrinkwrapPath, `${JSON.stringify(shrinkwrap, null, 2)}\n`, "utf8");

const minimatchRequire = createRequire(join(piRoot, "node_modules", "minimatch", "package.json"));
const resolvedEntry = realpathSync(minimatchRequire.resolve("brace-expansion"));
const resolvedRelative = relative(realpathSync(fixedPackage), resolvedEntry);
if (resolvedRelative === ".." || resolvedRelative.startsWith(`..${sep}`)) {
  throw new Error("Pi minimatch did not resolve the fixed brace-expansion package");
}
