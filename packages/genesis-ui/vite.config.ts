import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Genesis is served by the `cosmo genesis` aiohttp server out of the packaged
// `dist/` directory (same shape as prism-ui). Consequences:
//
//   * `base: "./"`  -  assets are referenced relatively so the bundle works no
//     matter what path the SPA is mounted under.
//   * the dev server proxies `/api` to a running `cosmo genesis` instance so
//     `npm run dev` gives a live folder-browser/init/scaffold API. Start the
//     CLI first (default port 7072), then `npm run dev`.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // A single JS chunk keeps the packaged asset list small and predictable
    // for the Python static handler.
    rollupOptions: {
      output: {
        entryFileNames: "assets/genesis.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
  server: {
    port: 5175,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:7072",
      },
    },
  },
});
