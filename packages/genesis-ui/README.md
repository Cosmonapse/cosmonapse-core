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
parse a Python module, spawn a process, or run `git`. The Python side does
those things and nothing else - it never templates HTML.

The four views are four lenses on one project: **Canvas** lays it out,
**Code** edits it, **Test** runs it, and **History** is the git panel - what
has changed since the last commit, and what it changed from. History exists
because most of what Genesis does is structural (adding a component writes a
module *and* rewrites `brain.py`), so the undo has to be the one people
already trust. It drives the user's own `git` and never commits, pushes or
pulls on its own.

The start screen has two tabs for the same reason: a project is either
already on this machine, or it is on GitHub. The **From git** tab connects an
account, lists the repositories that token can see, and clones one into a
folder you pick.

### About the token

Genesis never stores it. `POST /api/forge/connect` checks a pasted token
against the host, then hands it to `git credential approve`, which writes it
wherever the user's own `credential.helper` keeps secrets - Windows
Credential Manager, the macOS Keychain, libsecret. What Genesis persists is
`~/.cosmonapse/genesis.json`, holding a host and a login and no secret; when
it needs the token again (to list repositories) it asks git for it back with
`git credential fill`. So the credential that clones here is the same one a
terminal push finds, revoking it in one place revokes it everywhere, and
uninstalling Genesis leaves nothing behind.

A machine with **no** credential helper configured is a wall rather than a
warning: `git credential approve` would succeed and store nothing, so the
token would appear to save and then fail at the first push. The connect form
detects it and offers git's plaintext `store` helper as an explicit tick.

### What the network half will not do

`clone`, `fetch`, `push` and `pull` are the only functions that touch the
network, and they go through `_run_net` rather than `_run` - the one place
`GIT_TERMINAL_PROMPT=0`, `ssh -o BatchMode=yes` and the long timeout are
applied. Between them there is no path by which git stops and waits for a
human, which matters because a prompt would hang an aiohttp handler that has
no terminal to prompt on. A test asserts no networked verb reaches plain
`_run`.

`pull` is `--ff-only`. Merges and conflict resolution are deliberately out of
scope: a branch that has genuinely diverged needs a three-way editor, and
half of one is worse than sending you to a terminal that has a whole one.

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
| Version control | `/api/git`, `/api/git/init`, `/api/git/identity`, `/api/git/stage`, `/api/git/commit`, `/api/git/log`, `/api/git/show`, `/api/git/diff`, `/api/git/restore`, `/api/git/branches`, `/api/git/branch`, `/api/git/remote` |
| Network | `/api/git/clone`, `/api/git/push`, `/api/git/pull` |
| Git account | `/api/forge`, `/api/forge/connect`, `/api/forge/disconnect`, `/api/forge/repos` |

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
