# Prism UI

The browser visualization for the Cosmonapse Doppler. A Vite + React + TypeScript
single-page app. The built static bundle is shipped inside the `cosmonapse`
Python wheel and served by `cosmo doppler --prism`.

## How it fits together

```
packages/prism-ui/            ← this app (source of truth for the UI)
   └─ npm run build           → dist/  (static SPA: index.html + assets/)
        │  copy-dist.mjs
        ▼
packages/python-sdk/cosmo/commands/prism_dist/   ← bundled into the wheel
        ▲
   cosmo doppler --prism      → aiohttp serves prism_dist/ + the /ws bridge
```

The Python side never templates HTML. It serves the static files and exposes a
single WebSocket endpoint, `/ws?url=<synapse>&namespace=<ns>`, that streams one
JSON Signal envelope per message (see `src/types.ts`). That WS contract is the
entire API between the CLI bridge and this app.

## Develop with HMR

1. Start a Prism bridge from the SDK so the WS endpoint is live:

   ```bash
   cosmo doppler --prism            # serves on http://127.0.0.1:7071
   ```

2. In another terminal, run the Vite dev server (it proxies `/ws` to 7071):

   ```bash
   npm install
   npm run dev                      # http://127.0.0.1:5174
   ```

   Open the dev URL with a synapse query string, e.g.
   `http://127.0.0.1:5174/?url=cosmo://127.0.0.1:7070&namespace=dev`.

## Build for the wheel

```bash
npm run build:into-wheel
```

This runs `vite build` then copies `dist/` into
`../python-sdk/cosmo/commands/prism_dist/`. CI runs the same before building the
Python wheel, so `pip install cosmonapse` ships a prebuilt UI with no Node on the
install path.
