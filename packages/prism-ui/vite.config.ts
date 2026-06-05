import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Prism is served by the `cosmo doppler --prism` aiohttp server out of the
// packaged `dist/` directory. Two consequences shape this config:
//
//   * `base: "./"`  -  assets are referenced relatively so the bundle works no
//     matter what path the SPA is mounted under (root today, /prism later if
//     it ever moves onto the landing site).
//   * the dev server proxies `/ws` to a running `cosmo doppler --prism`
//     instance so `npm run dev` gives full HMR against live signals. Start the
//     CLI first (default port 7071), then `npm run dev`.
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
        entryFileNames: "assets/prism.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/ws": {
        target: "ws://127.0.0.1:7071",
        ws: true,
      },
    },
  },
});
