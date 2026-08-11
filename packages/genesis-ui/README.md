# Genesis UI

The browser workspace for building a Cosmonapse brain: name a project, scaffold
it, then grow it on a canvas or edit it as source. A Vite + React + TypeScript
single-page app. The built static bundle is shipped inside the `cosmonapse`
Python wheel and served by `cosmo genesis`.

## How it fits together

```
packages/genesis-ui/          ← this app (source of truth for the UI)
   └─ npm run build           → dist/  (static SPA: index.html + assets/)
        │  copy-dist.mjs
        ▼
packages/python-sdk/cosmo/commands/genesis_dist/   ← bundled into the wheel
        ▲
   cosmo genesis              → aiohttp serves genesis_dist/ + the local API
```

Unlike Prism, whose entire contract is one WebSocket, Genesis needs a real
local API: a browser cannot open a native folder dialog, run a scaffolder,
parse a Python module, or spawn a process. The Python side does those things
and nothing else - it never templates HTML.

The API is documented in the module docstring of
`packages/python-sdk/cosmo/commands/_genesis.py`, which is the authoritative
list. In outline:

| Group      | Routes                                                        |
| ---------- | ------------------------------------------------------------- |
| Project    | `/api/browse`, `/api/init`, `/api/detect`, `/api/scaffold`     |
| Source     | `/api/file` (GET/POST), `/api/helpers`, `/api/model`           |
| Components | `/api/component`, `/api/component/delete`, `/api/component/restore`, `/api/archived` |
| Editing    | `/api/declaration`, `/api/behavior`, `/api/behavior/delete`, `/api/engram-shape`, `/api/axon-source` |
| Running    | `/api/receptors`, `/api/brain`, `/api/brain/start`, `/api/brain/stop`, `/api/brain/ws`, `/api/receptor/http` |
| Infra      | `/api/synapse`, `/api/synapse/start`, `/api/synapse/stop`, `/api/prism` |

## Develop with HMR

1. Start a Genesis server from the SDK so the local API is live:

   ```bash
   cosmo genesis                    # serves on http://127.0.0.1:7072
   ```

2. Run the Vite dev server in this directory:

   ```bash
   npm install
   npm run dev
   ```

   `vite.config.ts` proxies `/api` and `/api/brain/ws` to port 7072, so the dev
   server gets the real backend with hot reload on the frontend.

## Ship a change

The dist is **not** rebuilt automatically when you edit `src/`. `cosmo genesis`
serves the bundle committed at `cosmo/commands/genesis_dist/`, so a source
change is invisible until you run:

```bash
npm run build:into-wheel     # vite build + copy-dist.mjs
```

and commit the result. The release workflow rebuilds both SPAs from source when
a tag is pushed, so the committed copy is a convenience for source checkouts
rather than the release artifact - but leaving it stale means anyone running
from a checkout sees an old UI.

If `genesis_dist/` is missing entirely, `cosmo genesis` serves a page saying so
rather than a blank screen. That is the expected state for a fresh clone that
has never built the UI.
