import { readFileSync } from "node:fs";
import { defineConfig } from "tsup";

// The published version lives only in package.json's `version`, which the
// release workflow sets from the `vX.Y.Z` git tag at publish time. We read it
// here and inline it as `__PKG_VERSION__` so the runtime `VERSION` export never
// has to be hand-edited.
const pkg = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
) as { version: string };

export default defineConfig({
  entry: ["src/index.ts"],
  format: ["esm", "cjs"],
  dts: true,
  clean: true,
  external: [
    "@modelcontextprotocol/sdk",
    "nats",
    "kafkajs",
    "better-sqlite3",
    "pg",
  ],
  define: {
    __PKG_VERSION__: JSON.stringify(pkg.version),
  },
});
