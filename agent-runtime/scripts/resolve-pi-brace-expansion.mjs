import { existsSync, lstatSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const runtimeRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const piRoot = join(runtimeRoot, "node_modules", "@earendil-works", "pi-coding-agent");
const nestedPackage = join(piRoot, "node_modules", "brace-expansion");
const fixedPackage = join(runtimeRoot, "node_modules", "brace-expansion");
const shrinkwrapPath = join(piRoot, "npm-shrinkwrap.json");
const fixed = {
  version: "5.0.9",
  resolved: "https://registry.npmjs.org/brace-expansion/-/brace-expansion-5.0.9.tgz",
  integrity: "sha512-ScQ4IuvIEF1TMlP7Zt+vjJ//9zlPb2SDcxWxM3bk8s6t6GGdJ7KO1dCcTidOPJKePW30LE/2cT7wCyPho9/Wxg==",
};

function packageVersion(path) {
  return JSON.parse(readFileSync(join(path, "package.json"), "utf8")).version;
}

if (packageVersion(fixedPackage) !== fixed.version) {
  throw new Error(`expected top-level brace-expansion ${fixed.version}`);
}

let nestedVersion;
if (existsSync(nestedPackage)) {
  const stat = lstatSync(nestedPackage);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("unexpected nested brace-expansion path");
  nestedVersion = packageVersion(nestedPackage);
}
if (nestedVersion !== undefined && nestedVersion !== "5.0.7") {
  throw new Error(`unexpected nested brace-expansion ${nestedVersion}`);
}

const shrinkwrap = JSON.parse(readFileSync(shrinkwrapPath, "utf8"));
const entry = shrinkwrap.packages?.["node_modules/brace-expansion"];
if (!entry || !["5.0.7", fixed.version].includes(entry.version)) {
  throw new Error("unexpected Pi brace-expansion shrinkwrap entry");
}

if (nestedVersion !== undefined) rmSync(nestedPackage, { recursive: true });
Object.assign(entry, fixed, { engines: { node: "20 || >=22" } });
writeFileSync(shrinkwrapPath, `${JSON.stringify(shrinkwrap, null, 2)}\n`, "utf8");
