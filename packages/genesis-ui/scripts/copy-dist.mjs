// Copies the Vite build output into the Python package so it ships in the wheel.
//
//   packages/genesis-ui/dist  →  packages/python-sdk/cosmo/commands/genesis_dist
//
// Run via `npm run build:into-wheel`. CI does the same before `hatch build`.
import { cp, rm, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "..", "dist");
const dest = resolve(here, "..", "..", "python-sdk", "cosmo", "commands", "genesis_dist");

try {
  await stat(src);
} catch {
  console.error(`[copy-dist] build output not found at ${src}. Run \`npm run build\` first.`);
  process.exit(1);
}

await rm(dest, { recursive: true, force: true });
await cp(src, dest, { recursive: true });
console.log(`[copy-dist] copied\n  ${src}\n→ ${dest}`);
