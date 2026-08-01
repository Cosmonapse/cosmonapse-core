"""
cosmo.commands._genesis
~~~~~~~~~~~~~~~~~~~~~~~~
Genesis - the "new brain" wizard: name a project, pick a folder, scaffold it
(same skeleton as `cosmo init`), then look at the result as a draw.io-style
canvas (one Synapse, the Neurons/Effectors/Engrams it hosts) or as source in
the Code tab.

Architecture
------------
An aiohttp app on a single port (default 7072) serves the Genesis single-page
app plus the small local API it needs (a browser can't open a native folder
dialog, run a scaffolder, or read a project off disk itself):

    GET  /                -> the Genesis SPA (index.html)
    GET  /assets/*        -> the SPA's static JS/CSS bundle
    GET  /api/browse      -> list subdirectories of a path (folder picker)
    POST /api/init        -> run the standard-skeleton scaffold at a path
    GET  /api/scaffold    -> read a scaffolded project back as graph nodes
    POST /api/component   -> add a Neuron/Effector/Engram module + wire it
    GET  /api/detect      -> can this folder be opened as a project?
    GET  /api/file        -> read one file out of a project (Code tab)
    POST /api/file        -> write one file back (helpers editor)
    POST /api/helpers     -> create helpers.py on demand
    GET  /api/model       -> parse a component into declaration + behaviours
    POST /api/declaration -> rewrite the declaration from the config form
    POST /api/behavior    -> add or replace one decorated behaviour
    POST /api/behavior/delete -> remove a behaviour
    POST /api/engram-shape    -> convert an Engram between shapes
    POST /api/axon-source     -> repoint an Axon at another Neuron source
    GET  /api/receptors       -> the receptors this project mounts, and how
                                 to talk to each one
    GET  /api/brain           -> is this project's brain.py running?
    POST /api/brain/start     -> spawn `python -u brain.py`
    POST /api/brain/stop      -> stop it
    GET  /api/brain/ws        -> WebSocket onto the brain's stdin/stdout
    POST /api/receptor/http   -> same-origin proxy to an HTTP Receptor
    GET  /api/synapse         -> is a synapse live for this namespace?
    POST /api/synapse/start   -> spawn a dev synapse on a chosen port
    POST /api/synapse/stop    -> stop the namespace on a running synapse
    POST /api/prism           -> open Prism on a live synapse

This mirrors cosmo/commands/_prism.py: same bundling approach
(``genesis_dist``, built by ``packages/genesis-ui``'s
``npm run build:into-wheel``), same "serve the prebuilt SPA, keep the Python
side to a thin local API" shape.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import keyword
import re
import time
import webbrowser
from importlib.resources import files as _pkg_files
from pathlib import Path

import click

from cosmo.commands import _genesis_ast as _ga
from cosmo.commands import _genesis_protocols as _gp
from cosmo.commands import _genesis_run as _gr
from cosmo.commands import _genesis_synapse as _gs
from cosmo.commands._shared import _HAS_RICH
from cosmo.commands.init import ScaffoldExistsError, scaffold_project

if _HAS_RICH:
    from rich.console import Console

    console = Console()

# ---------------------------------------------------------------------------
# Bundled SPA build location
# ---------------------------------------------------------------------------

def _genesis_dist_dir() -> Path | None:
    """Locate the bundled Genesis SPA build, or None if it was never built.

    The static assets are produced by ``npm run build:into-wheel`` in
    packages/genesis-ui and shipped inside this package as ``genesis_dist/``.
    Released wheels always contain it; a source checkout only has it after
    the UI has been built (see packages/genesis-ui/README or prism-ui's, the
    same pipeline).
    """
    try:
        root = Path(str(_pkg_files("cosmo.commands"))) / "genesis_dist"
    except (ModuleNotFoundError, TypeError):
        return None
    return root if (root / "index.html").is_file() else None


_MISSING_BUILD_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Genesis - not built</title>
<style>body{background:#07080c;color:#e6e7ec;font-family:ui-monospace,Menlo,monospace;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{max-width:560px;padding:32px;border:1px solid rgba(255,255,255,.12);border-radius:14px}
code{color:#22d3ee} h1{font-size:18px;margin:0 0 12px}</style></head>
<body><div class="box">
<h1>Genesis UI is not bundled in this install</h1>
<p>The static frontend was not found at <code>cosmo/commands/genesis_dist</code>.</p>
<p>Build it from the repo with:</p>
<p><code>cd packages/genesis-ui &amp;&amp; npm install &amp;&amp; npm run build:into-wheel</code></p>
<p>then reinstall the SDK (<code>pip install -e .</code>). Released wheels ship
the prebuilt UI, so this only appears for source checkouts that have not
built the UI yet.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Filesystem helpers backing the API
# ---------------------------------------------------------------------------

def _list_dir(raw_path: str | None) -> dict:
    """Directory listing for the folder picker: subdirectories of raw_path.

    Falls back to the user's home directory when raw_path is missing,
    doesn't exist, or isn't a directory - the form should always be able to
    recover to somewhere sane rather than error out.
    """
    path = Path(raw_path).expanduser().resolve() if raw_path else Path.home()
    if not path.is_dir():
        path = Path.home()

    entries = []
    try:
        children = sorted(
            path.iterdir(), key=lambda p: p.name.lower(),
        )
    except PermissionError:
        children = []

    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                entries.append({"name": child.name, "path": str(child)})
        except OSError:
            continue

    parent = str(path.parent) if path.parent != path else None
    return {"path": str(path), "parent": parent, "entries": entries}


# Best-effort neuron_id / effector_id extraction so the canvas can label
# nodes with their real identity instead of the bare filename. Falls back to
# the filename stem when the pattern isn't found - the scaffold's generated
# files always match it, but hand-edited ones might not.
_NEURON_ID_RE = re.compile(r'neuron_id\s*=\s*["\']([^"\']+)["\']')
_EFFECTOR_ID_RE = re.compile(r'effector_id\s*=\s*["\']([^"\']+)["\']')
_ENGRAM_ID_RE = re.compile(r'engram_id\s*=\s*["\']([^"\']+)["\']')
# Receptors take receptor_id= like the rest, but the scaffold's terminal.py
# leaves it at its default, so in practice the filename stem is the label -
# which is also the name a user would reach for anyway.
_RECEPTOR_ID_RE = re.compile(r'receptor_id\s*=\s*["\']([^"\']+)["\']')


def _extract_id(path: Path, pattern: re.Pattern) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return path.stem
    m = pattern.search(text)
    return m.group(1) if m else path.stem


def _package_modules(folder: Path) -> list[Path]:
    """Every module in a component package, including nested ones.

    Real projects group modules inside the package - the agent examples keep
    their Neurons in ``neurons/model/`` - so this walks the whole package
    rather than just its top level, which is what a plain ``glob("*.py")``
    would see (and which silently showed those projects as having no Neurons
    at all).
    """
    if not folder.is_dir():
        return []
    out = []
    for py in sorted(folder.rglob("*.py"), key=lambda p: str(p).lower()):
        if py.name == "__init__.py":
            continue
        if any(part in _SKIP_DIRS or part.startswith(".") for part in py.relative_to(folder).parts[:-1]):
            continue
        out.append(py)
    return out


def _module_nodes(folder: Path, pattern: re.Pattern) -> list[dict]:
    return [
        {
            "id": _extract_id(py, pattern),
            # Package-relative, posix-style: "model/coding.py" as well as
            # "hello.py", so the Code tab can address either.
            "file": py.relative_to(folder).as_posix(),
        }
        for py in _package_modules(folder)
    ]


def _project_files(target: Path) -> list[str]:
    """Every source file the Code tab can open, project-relative.

    Deliberately narrow: the component packages plus the top-level wiring
    and docs. Deep-walking an arbitrary folder would drag in .venv/, caches
    and whatever else lives next to the project.
    """
    out: list[str] = []
    for child in sorted(target.glob("*.py")):
        out.append(child.name)
    for name in ("README.md",):
        if (target / name).is_file():
            out.append(name)
    # _COMPONENT_DIRS, not _KIND_PACKAGE.values(): the Code tab opens every
    # package Genesis can *read*, which is a wider set than the ones it can
    # add to. receptors/ is readable today and not yet addable, and a node on
    # the canvas whose source you can't open would be a worse gap than the
    # missing Add button.
    for pkg in _COMPONENT_DIRS:
        folder = target / pkg
        if not folder.is_dir():
            continue
        if (folder / "__init__.py").is_file():
            out.append(f"{pkg}/__init__.py")
        for py in _package_modules(folder):
            out.append(f"{pkg}/{py.relative_to(folder).as_posix()}")
    return out


def _read_scaffold(raw_path: str) -> dict:
    """Read a scaffolded project directory back into Genesis's node shape."""
    target = Path(raw_path).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"{target} is not a directory")

    return {
        "project": target.name,
        "path": str(target),
        # The namespace config.py was scaffolded with. Carried on the
        # scaffold rather than asked for again, so the synapse form can
        # show it locked: the project already made this decision.
        "namespace": _gs.read_namespace(target),
        "synapse": {"id": target.name},
        "neurons": _module_nodes(target / "neurons", _NEURON_ID_RE),
        "effectors": _module_nodes(target / "effector", _EFFECTOR_ID_RE),
        "engrams": _module_nodes(target / "engram", _ENGRAM_ID_RE),
        "receptors": _module_nodes(target / "receptors", _RECEPTOR_ID_RE),
        "files": _project_files(target),
    }


# ---------------------------------------------------------------------------
# Importing an existing project
# ---------------------------------------------------------------------------
#
# Genesis has to answer "is this a Cosmonapse project?" about a folder it did
# not create. The reliable marker is not the file layout - projects can be
# laid out however their author likes - it is whether anything in the folder
# actually imports the SDK. Layout then decides how *much* of the project
# Genesis can show, which is what the warnings are for.

#: Directories never worth walking when looking for components.
_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
})

_IMPORTS_COSMONAPSE = re.compile(r"^\s*(?:from|import)\s+cosmonapse\b", re.MULTILINE)

#: A module-level or factory-returned component construction.
_DECLARES_COMPONENT = re.compile(
    r"(?:^|\s)(?:Axon|Effector\.serve|Engram\.serve|InMemoryEngram|"
    r"SqliteEngram|PostgresEngram)\s*\(",
)

_COMPONENT_DIRS = ("neurons", "effector", "engram", "receptors")


def _iter_py(root: Path, max_depth: int = 3):
    """Project .py files, skipping caches, virtualenvs and vendored trees."""
    def walk(folder: Path, depth: int):
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            return
        for child in children:
            try:
                if child.is_dir():
                    if depth < max_depth and child.name not in _SKIP_DIRS \
                            and not child.name.startswith("."):
                        yield from walk(child, depth + 1)
                elif child.suffix == ".py":
                    yield child
            except OSError:
                continue
    yield from walk(root, 0)


def _detect_project(raw_path: str) -> dict:
    """Decide whether a folder can be opened, and what opening it will show.

    Returns a verdict either way: ``is_project`` with a ``reason`` when the
    answer is no, so the start screen can say *why* a folder can't be opened
    instead of just refusing. ``warnings`` are never blocking - they describe
    what Genesis won't be able to do with this project once it's open.
    """
    target = Path(raw_path).expanduser().resolve() if raw_path else Path.home()

    def refuse(reason: str, children: bool = True) -> dict:
        # A refusal is more useful pointing somewhere than just saying no, so
        # it carries whatever projects sit one level down.
        return {
            "path": str(target), "name": target.name, "is_project": False,
            "reason": reason, "markers": [], "counts": {}, "warnings": [],
            "scaffolded": False,
            "children": _child_projects(target) if children else [],
        }

    if not target.is_dir():
        return refuse("That path isn't a folder.", children=False)

    py_files = list(_iter_py(target))
    if not py_files:
        return refuse("No Python files here.")

    importers: list[Path] = []
    declarers: list[Path] = []
    for py in py_files:
        try:
            body = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _IMPORTS_COSMONAPSE.search(body):
            importers.append(py)
            if _DECLARES_COMPONENT.search(body):
                declarers.append(py)

    if not importers:
        return refuse(
            f"Nothing in {target.name} imports cosmonapse, so this doesn't "
            "look like a Cosmonapse project.",
        )

    # Importing the SDK isn't enough on its own - a scratch folder of scripts
    # does that, and so does the SDK's own source tree. What makes a folder
    # openable is that it's laid out as a project: somewhere to put components,
    # or a brain.py that wires them.
    has_brain = (target / "brain.py").is_file()
    has_packages = any(_package_modules(target / d) for d in _COMPONENT_DIRS)
    if not (has_brain or has_packages):
        return refuse(
            f"{target.name} uses cosmonapse, but has no brain.py and no "
            "neurons/, effector/, engram/ or receptors/ package - there's no "
            "project here for Genesis to open.",
        )

    markers = [
        name for name in ("brain.py", "config.py", "demo.py", "helpers.py")
        if (target / name).is_file()
    ]
    markers += [f"{d}/" for d in _COMPONENT_DIRS if (target / d).is_dir()]

    scaffold = _read_scaffold(str(target))
    counts = {
        "neurons": len(scaffold["neurons"]),
        "engrams": len(scaffold["engrams"]),
        "effectors": len(scaffold["effectors"]),
        "receptors": len(scaffold["receptors"]),
    }

    return {
        "path": str(target),
        "name": target.name,
        "is_project": True,
        "reason": None,
        "markers": markers,
        "counts": counts,
        "warnings": _import_warnings(target, scaffold, declarers),
        "scaffolded": has_brain and has_packages,
        "children": [],
    }


def _child_projects(target: Path, limit: int = 60) -> list[dict]:
    """Immediate subfolders that do look like projects.

    Pointing Genesis at a folder of projects is at least as common as pointing
    it at one, so a refusal offers the way forward instead of a dead end. The
    limit only exists to bound the work in a directory with thousands of
    entries - each candidate costs a scaffold read - and is set well above
    any plausible number of projects sitting side by side.
    """
    out: list[dict] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except (PermissionError, OSError):
        return out
    for child in children:
        if len(out) >= limit:
            break
        try:
            if not child.is_dir() or child.name in _SKIP_DIRS or child.name.startswith("."):
                continue
        except OSError:
            continue
        try:
            looks_like = ((child / "brain.py").is_file()
                          or any(_package_modules(child / d) for d in _COMPONENT_DIRS))
        except OSError:
            continue          # unreadable subdirectory - not ours to report on
        if not looks_like:
            continue
        try:
            scaffold = _read_scaffold(str(child))
        except (OSError, FileNotFoundError):
            continue
        out.append({
            "name": child.name,
            "path": str(child),
            "counts": {
                "neurons": len(scaffold["neurons"]),
                "engrams": len(scaffold["engrams"]),
                "effectors": len(scaffold["effectors"]),
                "receptors": len(scaffold["receptors"]),
            },
        })
    return out


def _import_warnings(target: Path, scaffold: dict, declarers: list[Path]) -> list[dict]:
    """What Genesis won't be able to do with this project, said plainly."""
    out: list[dict] = []

    if not (target / "brain.py").is_file():
        out.append({
            "id": "no-brain",
            "text": "No brain.py, so new components will be written but not wired up - "
                    "you'll need to attach them wherever this project does its wiring.",
        })

    total = sum(len(scaffold[k]) for k in ("neurons", "effectors", "engrams", "receptors"))
    if total == 0:
        out.append({
            "id": "no-components",
            "text": "No components found in neurons/, effector/, engram/ or receptors/ "
                    "yet. The canvas will just show the synapse until you add one.",
        })

    # Components Genesis can see in the file system but not on the canvas,
    # because it only reads the three standard folders.
    stray = [
        str(p.relative_to(target)).replace("\\", "/")
        for p in declarers
        if p.parent != target / "neurons"
        and p.parent != target / "effector"
        and p.parent != target / "engram"
        and p.parent != target / "receptors"
    ]
    if stray:
        shown = ", ".join(stray[:4]) + (f" and {len(stray) - 4} more" if len(stray) > 4 else "")
        out.append({
            "id": "stray-components",
            "text": f"Genesis reads components from neurons/, effector/, engram/ and "
                    f"receptors/ only, so these won't appear on the canvas: {shown}.",
        })

    # Modules that build their component in a factory can be configured but
    # not given behaviour - worth knowing before you go looking for the button.
    factories = []
    for pkg in _COMPONENT_DIRS:
        folder = target / pkg
        if not folder.is_dir():
            continue
        for py in sorted(folder.glob("*.py")):
            if py.name == "__init__.py":
                continue
            try:
                model = _ga.parse_component(py.read_text(encoding="utf-8", errors="ignore"))
            except _ga.EditError:
                continue
            decl = model.get("declaration")
            if decl and decl.get("scope") == "factory":
                factories.append(f"{pkg}/{py.name}")
    if factories:
        out.append({
            "id": "factory-components",
            "text": f"{', '.join(factories)} build their component inside a factory. "
                    "You can edit the configuration, but behaviours can't be added "
                    "from here - there's no module-level object to decorate.",
        })

    return out


# ---------------------------------------------------------------------------
# Reading one file back (the Code tab)
# ---------------------------------------------------------------------------

# A generous ceiling: source modules are kilobytes, and the Code tab renders
# whatever it gets in one pane. Anything past this is not something a human
# is reading in a browser.
_MAX_FILE_BYTES = 512 * 1024


def _read_project_file(raw_path: str, rel: str) -> dict:
    """Read `rel` out of the project at `raw_path`.

    Confined to the project directory: the resolved file must still live
    under the resolved project root, so `../../.ssh/id_rsa` and friends are
    rejected even though this server only ever listens on 127.0.0.1.
    """
    target = Path(raw_path).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"{target} is not a directory")

    candidate = (target / rel).resolve()
    if candidate != target and target not in candidate.parents:
        raise PermissionError("path escapes the project directory")
    if not candidate.is_file():
        raise FileNotFoundError(f"{rel} not found in {target.name}")

    size = candidate.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise ValueError(f"{rel} is {size} bytes - too large to display")

    return {
        "path": str(candidate),
        "file": rel,
        "text": candidate.read_text(encoding="utf-8", errors="replace"),
    }


# ---------------------------------------------------------------------------
# Adding a component (new module + brain.py wiring)
# ---------------------------------------------------------------------------
#
# The four primitives, and everything that differs between them: which
# package the module lands in, what the module exports, and which Dendrite
# method attaches it. Neurons think, Engrams remember, Effectors act,
# Receptors listen.

_KIND_PACKAGE = {
    "neuron": "neurons",
    "effector": "effector",
    "engram": "engram",
    "receptor": "receptors",
}
_KIND_EXPORT = {
    "neuron": "AXON",
    "effector": "EFFECTOR",
    "engram": "ENGRAM",
    "receptor": "RECEPTOR",
}
_KIND_ATTACH = {
    "neuron": "attach_axon",
    "effector": "attach_effector",
    "engram": "attach_engram",
    "receptor": "attach_receptor",
}

#: Receptors are the one kind whose template depends on a second choice: the
#: three classes take different constructor keywords and expose different
#: decorators, so the flavour is picked when the module is created and is not
#: switchable in place afterwards.
_RECEPTOR_DEFAULT_SHAPE = "cli"
_RECEPTOR_PATH = {"api": "/dispatch", "chat": "/chat"}

# Component ids are used verbatim as bus identities, so keep them to the
# lowercase-kebab shape the rest of the ecosystem uses. The module name is
# the same string with dashes swapped for underscores.
_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")

_PY_KEYWORDS = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist)


_NEW_NEURON_PY = '''"""__NAME__ - a Neuron: an async function, zero protocol knowledge.

Swap the body for a model call whenever you're ready - the unified factory
wraps any source behind the same NeuronFn contract:

    from cosmonapse import Neuron
    fn = Neuron(source="ollama", model="llama3")
"""
from cosmonapse import Axon


async def __MODULE__(input: dict, context: list) -> dict:
    return {"echo": input}


# The Axon gives the Neuron an identity on the bus.
AXON = Axon(
    neuron_id="__NAME__",
    neuron_fn=__MODULE__,
    capabilities=[],
    version="0.0.1",
)
'''


_NEW_EFFECTOR_PY = '''"""__NAME__ - an Effector: one @EFFECTOR.on_tool_call hook, zero protocol
knowledge. A TOOL_CALL arrives, your handler runs, its return value is
emitted as the TOOL_RESULT.

Subclass cosmonapse.effector.base.Effector instead of Effector.serve() when
this tool family needs its own connect()/close() lifecycle - a subprocess,
an HTTP pool, a spawned MCP server.
"""
from cosmonapse import Effector

EFFECTOR = Effector.serve(
    effector_id="__NAME__",
    effector_kind="__MODULE__",
)


@EFFECTOR.on_tool_call
async def handle(tool: str, args: dict):
    if tool == "ping":
        return {"pong": args}
    return None   # unhandled -> "unhandled tool" error on TOOL_RESULT
'''


_NEW_ENGRAM_PY = '''"""__NAME__ - an Engram: the memory Neurons RECALL from and IMPRINT to.

Two layers, because the SDK separates storage from hooks:

  * ``_backend`` is a finished backend - InMemoryEngram implements recall()
    and imprint() as real methods, so it works the moment you run it. Swap
    it for SqliteEngram or PostgresEngram (same constructor shape) when the
    memory should outlive the process.
  * ``ENGRAM`` is the served front, whose read and write surfaces are
    decorators. That is what gives this module somewhere to put behaviour -
    a cache, an ACL, a quota, a rewrite of the query - in front of the
    storage, without either layer knowing about the other.

The handlers below just forward. Delete them to answer from somewhere else
entirely, or put your own logic above the forward and return None from it to
fall through to the storage.
"""
from cosmonapse import Engram, InMemoryEngram

_backend = InMemoryEngram(
    engram_id="__NAME__",
    engram_kind="keyvalue",
)

ENGRAM = Engram.serve(
    engram_id="__NAME__",
    engram_kind="keyvalue",
)


@ENGRAM.on_recall
async def recall(query, **kw):
    return await _backend.recall(query, **kw)


@ENGRAM.on_imprint
async def imprint(op, entry, **kw):
    return await _backend.imprint(op, entry, **kw)
'''

_NEW_RECEPTOR_CLI_PY = '''"""__NAME__ - a CliReceptor: a typed command becomes a TASK.

A command function *returns the TASK input* - that is the whole contract.
The argparse tree and the REPL are derived from its signature:

    no default        -> positional  (a str one takes the rest of the line)
    default           -> --flag, typed from the annotation
    bool default      -> --flag (store_true)

`local=True` marks a command answered right here that never dispatches.

Built with no dendrite= on purpose; brain.py binds it when it attaches.
"""
from cosmonapse import CliReceptor

RECEPTOR = CliReceptor(
    __TARGET__
    prog="__NAME__",
    description="Talk to this brain from a terminal.",
    timeout_s=30.0,
)


@RECEPTOR.command(help="send a goal to the neuron")
def ask(text: str):
    return {"prompt": text}        # <- the TASK input


@RECEPTOR.on_result
def render(sig):
    """Terminal Signal -> what the terminal prints."""
    return sig.payload["output"]
'''


_NEW_RECEPTOR_API_PY = '''"""__NAME__ - an ApiReceptor: one HTTP endpoint, all three dispatch modes.

    POST __PATH__  {"input": ..., "mode": "send"|"wait"|"stream"}

`mode` picks how the turn is answered: send returns as soon as the TASK is
on the bus, wait blocks for the terminal Signal, stream yields Signals as
they arrive. A request may only ask for a mode in allowed_modes=.

Needs the optional extra:  pip install 'cosmonapse[receptor]'

Built with no dendrite= on purpose - an ASGI app is imported before an event
loop exists, so brain.py binds it in the lifespan when it attaches.
"""
from cosmonapse import ApiReceptor

RECEPTOR = ApiReceptor(
    __TARGET__
    path="__PATH__",
    port=8000,
    timeout_s=60.0,
)


@RECEPTOR.route("/status", methods=["GET"])
async def status():
    """An ordinary FastAPI route, mounted alongside the dispatch endpoint."""
    return {"ok": True}


@RECEPTOR.on_result
def render(sig):
    """Terminal Signal -> the response body."""
    return sig.payload["output"]
'''


_NEW_RECEPTOR_CHAT_PY = '''"""__NAME__ - a ChatReceptor: one turn, one dispatch, plus a served page.

Serves a chat UI at __PATH__ and answers its turns on the same port. Each
session carries history_turns= of prior turns; the turn is recorded *after*
dispatch, so `history` holds prior turns only.

voice=True enables speech in the served page. That is client-side only - the
browser's Web Speech API - so no audio crosses the wire and nothing about
voice appears in the protocol.

Needs the optional extra:  pip install 'cosmonapse[receptor]'
"""
from cosmonapse import ChatReceptor

RECEPTOR = ChatReceptor(
    __TARGET__
    path="__PATH__",
    title="__NAME__",
    greeting="Ask me something.",
    voice=False,
    history_turns=8,
    port=8000,
)


@RECEPTOR.on_result
def render(sig):
    """Terminal Signal -> what the page shows."""
    return sig.payload["output"]
'''


def _receptor_target(target: Path) -> str:
    """The dispatch target a newly written Receptor is born with.

    A convenience, not a requirement. All three targeting shapes are legal:
    ``neuron=`` addresses one, ``capabilities=[...]`` routes to whoever covers
    them, and *neither* is an open call - broadcast, answered by any
    ``catch_all=True`` Axon or unfiltered ``@on_task_signal`` observer in the
    namespace.

    Which makes this purely about the first run. The scaffold's ``hello`` Axon
    is an ordinary one, so an open call from a freshly generated Receptor
    would be heard by nobody and surface as a dispatch timeout. Pointing the
    module at a Neuron the project actually has means it answers the moment it
    is attached, and the two other shapes are a one-line edit away - which the
    generated comment says.
    """
    neurons = _module_nodes(target / "neurons", _NEURON_ID_RE)
    ids = [n["id"] for n in neurons if n["id"]]
    if ids:
        return (f'neuron="{ids[0]}",'.ljust(31)
                + '# addressed; or capabilities=["..."]')
    # No Neuron to point at: leave it an open call and name the three shapes,
    # since which one is wanted is a design choice Genesis cannot make.
    return ("neuron=None,".ljust(31)
            + '# open call; or neuron="<id>" / capabilities=[...]')


_KIND_TEMPLATE = {
    "neuron": _NEW_NEURON_PY,
    "effector": _NEW_EFFECTOR_PY,
    "engram": _NEW_ENGRAM_PY,
}

#: Keyed by shape rather than kind - see _RECEPTOR_DEFAULT_SHAPE.
_RECEPTOR_TEMPLATE = {
    "cli": _NEW_RECEPTOR_CLI_PY,
    "api": _NEW_RECEPTOR_API_PY,
    "chat": _NEW_RECEPTOR_CHAT_PY,
}

_KIND_INIT_DOC = {
    "neuron": '"""Neuron modules - each exposes an AXON (or a make_axon factory)."""\n',
    "effector": '"""Effector modules - each exposes an EFFECTOR (Effector.serve(), or a\n'
                "subclass of cosmonapse.effector.base.Effector for a tool family that needs\n"
                'its own connect()/close() lifecycle)."""\n',
    "engram": '"""Engram modules - each exposes an ENGRAM (InMemoryEngram, SqliteEngram,\n'
              'PostgresEngram, or Engram.serve())."""\n',
    "receptor": '"""Receptor modules - each exposes a RECEPTOR (CliReceptor, ApiReceptor\n'
                'or ChatReceptor), built unbound; brain.py binds it on attach."""\n',
}


class ComponentError(Exception):
    """Raised when a component can't be created (bad name, name taken, ...).

    ``exists`` distinguishes "that module is already there" (the caller can
    offer to overwrite) from "that name isn't usable" (it can't).
    """

    def __init__(self, message: str, *, exists: bool = False) -> None:
        super().__init__(message)
        self.exists = exists


def _module_name(name: str) -> str:
    return name.replace("-", "_")


def _validate(kind: str, name: str, shape: str = "") -> tuple[str, str]:
    if kind not in _KIND_PACKAGE:
        raise ComponentError(f"unknown kind {kind!r}")
    if kind == "receptor" and shape and shape not in _RECEPTOR_TEMPLATE:
        raise ComponentError(
            f"unknown receptor type {shape!r} - one of "
            f"{', '.join(_RECEPTOR_TEMPLATE)}.",
        )
    name = name.strip().lower()
    if not _NAME_RE.match(name):
        raise ComponentError(
            "Use a lowercase name of letters, digits and dashes, "
            "starting with a letter - e.g. summarize-notes."
        )
    module = _module_name(name)
    if module in _PY_KEYWORDS:
        raise ComponentError(f"{name!r} is a Python keyword.")
    return name, module


# A wiring line already in build_worker, e.g. "    worker.attach_axon(...)".
# Matching an existing one is how the wiring learns the receiver variable
# and indentation actually used, instead of assuming "worker". The attach
# verb is captured too, because *which* Dendrite a component belongs on
# depends on its kind - see _anchor_for.
_ATTACH_RE = re.compile(
    r"^(\s*)(\w+)\.attach_(axon|effector|engram|receptor)\(", re.MULTILINE,
)
_RETURN_RE = re.compile(r"^(\s+)return (\w+)\s*$", re.MULTILINE)
_IMPORT_RE = re.compile(r"^(?:from [\w.]+ import .+|import [\w.]+)$", re.MULTILINE)
_DEF_RE = re.compile(r"^def (\w+)\s*\(", re.MULTILINE)
_WORKER_ROLE_RE = re.compile(r"""role\s*=\s*['"]worker['"]""")

#: Axons, Effectors and Engrams are hosted; Receptors dispatch. A
#: ``role="worker"`` Dendrite is explicitly not allowed to dispatch
#: (``Dendrite._require_orchestrator``), so mounting a Receptor on one
#: produces a brain that raises on its first turn. The two families
#: therefore want different receivers, which is the whole point of
#: _anchor_for.
_INTERFACE_KINDS = {"receptor"}


def _builders(src: str) -> list[dict]:
    """Every top-level ``def`` in brain.py that returns a named Dendrite.

    Line-based like the rest of _wire_brain: brain.py is the one file in the
    skeleton people hand-edit, so this reads the shape that is there rather
    than assuming the scaffold's. ``worker`` is taken from the literal
    ``role="worker"`` in the constructor, which is also how a reader of the
    file tells the two sides apart.
    """
    out: list[dict] = []
    defs = list(_DEF_RE.finditer(src))
    for i, m in enumerate(defs):
        start = m.start()
        end = defs[i + 1].start() if i + 1 < len(defs) else len(src)
        body = src[start:end]
        ret = _RETURN_RE.search(body)
        if ret is None:          # main(), helpers - not a builder
            continue
        out.append({
            "name": m.group(1),
            "indent": ret.group(1),
            "var": ret.group(2),
            "at": start + ret.start(),
            "worker": bool(_WORKER_ROLE_RE.search(body)),
        })
    return out


def _anchor_for(src: str, kind: str) -> tuple[str, str, int] | None:
    """Where to insert the attach line for ``kind``: (indent, receiver, offset).

    Preference order, and why:

    1. **The last attach of the same family.** A project that already hosts
       Axons on ``worker`` and Receptors on ``edge`` has answered the question
       itself; copy its answer. Family, not "last attach anywhere", is the fix
       for the scaffold - its final attach is ``edge.attach_receptor(...)``,
       so last-wins put every new Neuron on the orchestrator.
    2. **A builder of the right side.** Nothing of this family attached yet,
       so fall back to the Dendrite whose role fits: the ``role="worker"`` one
       for Axons/Effectors/Engrams, a non-worker one for Receptors.
    3. **Nothing.** Better to hand the user a module and tell them to wire it
       than to write a line that makes brain.py raise at runtime.
    """
    want_interface = kind in _INTERFACE_KINDS

    family = [m for m in _ATTACH_RE.finditer(src)
              if (m.group(3) == "receptor") is want_interface]
    if family:
        last = family[-1]
        return last.group(1), last.group(2), src.index("\n", last.end()) + 1

    builders = _builders(src)
    fitting = [b for b in builders if b["worker"] is not want_interface]
    if not fitting:
        return None
    b = fitting[0]
    return b["indent"], b["var"], b["at"]


#: A brain.py written by the current scaffolder runs a *brain*, not a
#: Dendrite: one node per component, all handed to run_brain. Adding a
#: component there means a new builder plus a new argument - not a line
#: inserted into somebody else's builder, which is what _anchor_for does
#: and which would silently host the new component on an unrelated node.
_RUN_BRAIN_RE = re.compile(r"\brun_brain\(", re.MULTILINE)
_BUILDER_ARG_RE = re.compile(r"^(\s*)build_(\w+)\(synapse\),[ \t]*$", re.MULTILINE)
_ENTRY_DEF_RE = re.compile(r"^(?:def _stop_on_signals|async def main)\b", re.MULTILINE)

#: role= and the attach call differ per kind; everything else is shared.
#: Receptors get the orchestrator role (attach_receptor refuses a worker)
#: and a registry_store, so find_neurons - a CliReceptor's `ping` - works.
_NODE_BUILDER = {
    "neuron": ('        dendrite_id="{mod}-node", role="worker",\n',
               "attach_axon", "AXON", "thinks"),
    "engram": ('        dendrite_id="{mod}-node", role="worker",\n',
               "attach_engram", "ENGRAM", "remembers"),
    "effector": ('        dendrite_id="{mod}-node", role="worker",\n',
                 "attach_effector", "EFFECTOR", "acts"),
    "receptor": (
        ('        dendrite_id="{mod}-node", heartbeat_s=0,\n'
         "        registry_store=MemoryRegistryStore(),\n"),
        "attach_receptor", "RECEPTOR", "listens",
    ),
}


def _imports(src: str, pkg: str, module: str):
    return re.search(
        rf"^from {re.escape(pkg)} import .*\b{re.escape(module)}\b",
        src, re.MULTILINE,
    )


def _add_import(src: str, pkg: str, module: str, import_line: str):
    """Insert ``import_line`` if absent. Returns the source, or None if
    brain.py has no import block to sit in."""
    if _imports(src, pkg, module):
        return src
    # Sit next to the sibling imports from the same package when there are
    # any, otherwise after the last import in the header block.
    anchor = None
    for m in re.finditer(rf"^from {re.escape(pkg)} import .+$", src, re.MULTILINE):
        anchor = m
    if anchor is None:
        for m in _IMPORT_RE.finditer(src):
            anchor = m
    if anchor is None:
        return None
    at = src.index("\n", anchor.end()) + 1
    return src[:at] + import_line + "\n" + src[at:]


def _wire_node(src: str, kind: str, module: str) -> tuple[str, str] | None:
    """Add a builder for ``module`` and pass it to run_brain. (src, note)."""
    spec = _NODE_BUILDER.get(kind)
    if spec is None:
        return None
    ident, attach, export, verb = spec

    args = list(_BUILDER_ARG_RE.finditer(src))
    if not args:
        return None
    if re.search(rf"^def build_{re.escape(module)}\b", src, re.MULTILINE):
        return src, "already wired"

    builder = (
        f"def build_{module}(synapse) -> Dendrite:\n"
        f'    """The node {module} {verb} on."""\n'
        f"    node = Dendrite(\n"
        f"        synapse=synapse, namespace=NAMESPACE,\n"
        f"{ident.format(mod=module)}"
        f"    )\n"
        f"    node.{attach}({module}.{export})\n"
        f"    return node\n\n\n"
    )

    # The builder goes above the entry points, with the other builders.
    entry = _ENTRY_DEF_RE.search(src)
    at = entry.start() if entry else len(src)
    src = src[:at] + builder + src[at:]

    # ...and its argument after the last existing one. Re-find: the insert
    # above shifted every offset.
    args = list(_BUILDER_ARG_RE.finditer(src))
    last = args[-1]
    line_end = src.index("\n", last.end()) + 1
    src = (src[:line_end]
           + f"{last.group(1)}build_{module}(synapse),\n"
           + src[line_end:])
    return src, "wired into brain.py as its own node"


def _wire_brain(target: Path, kind: str, module: str) -> tuple[bool, str]:
    """Add the import + attach line for a new module to brain.py.

    Line-based on purpose: brain.py is the one file in the skeleton people
    hand-edit, so this reads the shape that's actually there (the receiver
    variable and indent of the existing attach calls) rather than rewriting
    the file from a template. Which receiver is _anchor_for's job, and it is
    kind-aware: hosted components go on the worker, Receptors on the
    dispatching side. Returns (wired, note); a False just means the caller
    should tell the user to wire it themselves - never an error, the module
    on disk is the real deliverable.
    """
    brain = target / "brain.py"
    if not brain.is_file():
        return False, "no brain.py in this project"

    try:
        src = brain.read_text(encoding="utf-8")
    except OSError as e:
        return False, str(e)

    pkg = _KIND_PACKAGE[kind]
    import_line = f"from {pkg} import {module}"
    export = _KIND_EXPORT[kind]

    # A per-node brain (run_brain + build_* arguments) needs a whole builder,
    # not a line inside one. Older projects - a shared worker and edge, or a
    # hand-rolled shape - fall through to _anchor_for below.
    if _RUN_BRAIN_RE.search(src):
        wired = _wire_node(src, kind, module)
        if wired is not None:
            src, note = wired
            if note != "already wired":
                src = _add_import(src, pkg, module, import_line)
            try:
                brain.write_text(src, encoding="utf-8")
            except OSError as e:
                return False, str(e)
            return True, note

    anchor = _anchor_for(src, kind)
    if anchor is None:
        return False, (
            "brain.py has no Dendrite to mount a Receptor on - Receptors "
            "dispatch, so they need the orchestrator side, not a "
            'role="worker" one. Attach it yourself.'
            if kind in _INTERFACE_KINDS else
            "couldn't find where to attach it in brain.py"
        )
    indent, receiver, insert_at = anchor

    attach_line = f"{indent}{receiver}.{_KIND_ATTACH[kind]}({module}.{export})\n"

    already_attached = attach_line.strip() in src
    if already_attached and _imports(src, pkg, module):
        return True, "already wired"

    if not already_attached:
        src = src[:insert_at] + attach_line + src[insert_at:]

    src = _add_import(src, pkg, module, import_line)
    if src is None:
        return False, "couldn't find the import block in brain.py"

    try:
        brain.write_text(src, encoding="utf-8")
    except OSError as e:
        return False, str(e)
    return True, "wired into brain.py"


def _create_component(raw_path: str, kind: str, raw_name: str, force: bool = False,
                      shape: str = "") -> dict:
    """Write a new component module and wire it into brain.py.

    ``shape`` is only meaningful for Receptors, where it picks which of the
    three classes the module is written with. It is ignored for the other
    kinds, whose template is decided by the kind alone.
    """
    target = Path(raw_path).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"{target} is not a directory")

    name, module = _validate(kind, raw_name, shape)
    pkg = _KIND_PACKAGE[kind]
    folder = target / pkg
    folder.mkdir(parents=True, exist_ok=True)

    init = folder / "__init__.py"
    if not init.exists():
        init.write_text(_KIND_INIT_DOC[kind], encoding="utf-8")

    dest = folder / f"{module}.py"
    if dest.exists() and not force:
        raise ComponentError(f"{pkg}/{module}.py already exists.", exists=True)

    if kind == "receptor":
        shape = shape or _RECEPTOR_DEFAULT_SHAPE
        template = _RECEPTOR_TEMPLATE[shape]
    else:
        shape = ""
        template = _KIND_TEMPLATE[kind]

    body = (
        template
        .replace("__NAME__", name)
        .replace("__MODULE__", module)
        .replace("__PATH__", _RECEPTOR_PATH.get(shape, f"/{module}"))
        .replace("__TARGET__", _receptor_target(target))
    )
    dest.write_text(body, encoding="utf-8")

    wired, note = _wire_brain(target, kind, module)
    return {
        "kind": kind,
        "shape": shape,
        "id": name,
        "file": f"{module}.py",
        "path": f"{pkg}/{module}.py",
        "wired": wired,
        "note": note,
    }


# ---------------------------------------------------------------------------
# The Test tab: what this project mounts, and how to drive each one
# ---------------------------------------------------------------------------
#
# Everything here is read off the *source*, not off a running process. That
# is the whole reason the Test tab can show you a receptor list before you
# have started anything - and it is why a CliReceptor is drivable at all: its
# surface is a set of decorated functions, which a browser could never
# discover over HTTP but Genesis can simply read.
#
# Where a keyword isn't set, the SDK default is reported, because that is what
# the receptor will actually use. A panel that showed "port: unset" would be
# technically true and useless.

_RECEPTOR_DEFAULTS = {
    "cli": {"prompt": "> "},
    "api": {"path": "/dispatch", "host": "127.0.0.1", "port": 8000},
    "chat": {"path": "/chat", "host": "127.0.0.1", "port": 8000,
             "title": "Cosmonapse Chat", "greeting": "Ask me something.",
             "voice": False, "history_turns": 8},
}


def _command_params(signature: str) -> list[dict]:
    """A command's parameters, and what each becomes on the command line.

    The CliReceptor derives its argparse tree from the signature - no default
    is a positional, a default is a --flag typed from the annotation, and a
    bool default is store_true. Reproducing that mapping here is what lets the
    palette show a command's real usage without running anything.
    """
    try:
        fn = ast.parse(f"def _f({signature}): pass").body[0]
    except SyntaxError:
        return []
    args = list(getattr(fn.args, "posonlyargs", [])) + list(fn.args.args)
    defaults = list(fn.args.defaults)
    # defaults align to the tail of the positional list
    pad = [None] * (len(args) - len(defaults))
    out = []
    for arg, default in zip(args, pad + defaults, strict=True):
        if arg.arg == "self":
            continue
        annotation = ast.unparse(arg.annotation) if arg.annotation else ""
        has_default = default is not None
        rendered = ast.unparse(default) if has_default else ""
        is_bool = annotation == "bool" or rendered in ("True", "False")
        out.append({
            "name": arg.arg,
            "annotation": annotation,
            "default": rendered,
            "required": not has_default,
            "form": "positional" if not has_default else "flag" if not is_bool else "switch",
        })
    for kwarg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True):
        annotation = ast.unparse(kwarg.annotation) if kwarg.annotation else ""
        rendered = ast.unparse(default) if default is not None else ""
        is_bool = annotation == "bool" or rendered in ("True", "False")
        out.append({
            "name": kwarg.arg,
            "annotation": annotation,
            "default": rendered,
            "required": default is None,
            "form": "switch" if is_bool else "flag",
        })
    return out


def _receptor_entry(target: Path, rel: str) -> dict | None:
    """One receptor module, as the Test tab needs it."""
    try:
        text = (target / rel).read_text(encoding="utf-8")
        model = _ga.parse_component(text)
    except (OSError, _ga.EditError):
        return None
    decl = model.get("declaration")
    if not decl or decl.get("kind") != "receptor":
        return None

    shape = model.get("shape") or ""
    set_fields = {f["name"]: f["value"] for f in decl.get("fields", [])}
    config = dict(_RECEPTOR_DEFAULTS.get(shape, {}))
    config.update({k: v for k, v in set_fields.items() if v is not None})

    commands = []
    if shape == "cli":
        for b in model.get("behaviors", []):
            if b.get("protocol") != "command":
                continue
            args = b.get("args", {})
            # The decorator's name= wins; without it the function name is the
            # command, with underscores swapped for dashes - the SDK's rule.
            named = args.get("name", {}).get("value")
            commands.append({
                "name": named or b["fn_name"].replace("_", "-"),
                "help": args.get("help", {}).get("value") or "",
                "local": bool(args.get("local", {}).get("value", False)),
                "is_default": bool(args.get("default", {}).get("value", False)),
                "fn_name": b["fn_name"],
                "params": _command_params(b.get("signature", "")),
            })

    module = rel.split("/")[-1]
    return {
        "id": _extract_id(target / rel, _RECEPTOR_ID_RE),
        "file": module,
        "path": rel,
        "shape": shape,
        "callee": decl.get("callee", ""),
        # Only ever "cosmonapse[receptor]" territory for the HTTP two; the UI
        # uses this to explain an ImportError before the user hits it.
        "needs_extra": shape in ("api", "chat"),
        "neuron": set_fields.get("neuron") or "",
        "capabilities": set_fields.get("capabilities") or [],
        "config": config,
        "commands": commands,
    }


def _read_receptors(raw_path: str) -> dict:
    """Every receptor this project mounts, in file order."""
    target = Path(raw_path).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"{target} is not a directory")
    folder = target / "receptors"
    entries = []
    for py in _package_modules(folder):
        rel = f"receptors/{py.relative_to(folder).as_posix()}"
        entry = _receptor_entry(target, rel)
        if entry is not None:
            entries.append(entry)
    return {
        "path": str(target),
        "has_brain": (target / "brain.py").is_file(),
        "receptors": entries,
    }


def _receptor_base_url(entry: dict) -> str:
    cfg = entry.get("config", {})
    host = str(cfg.get("host") or "127.0.0.1")
    # 0.0.0.0 means "every interface" to a server and nothing to a client.
    if host in ("0.0.0.0", "::"):  # noqa: S104 - detecting it, not binding to it
        host = "127.0.0.1"
    return f"http://{host}:{int(cfg.get('port') or 8000)}"


# ---------------------------------------------------------------------------
# helpers.py - the shared module every component can import
# ---------------------------------------------------------------------------

HELPERS_FILE = "helpers.py"

_HELPERS_PY = '''"""Shared helper functions for this project.

Every component can import from here - entries run from the project root, so
``from helpers import ...`` resolves from neurons/, effector/, engram/ and
receptors/ alike::

    from helpers import shorten

    async def summarise(input: dict, context: list) -> dict:
        return {"summary": shorten(input["text"])}

This is ordinary Python with no protocol involvement: nothing in here is a
Neuron, an Engram, an Effector or a Receptor, which is the point - the
primitives stay
about the bus, and plain logic stays plain.
"""


def shorten(text: str, limit: int = 280) -> str:
    """Trim text to `limit` characters, on a word boundary where possible."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"
'''


def _ensure_helpers(raw_path: str) -> dict:
    """Create helpers.py if the project doesn't have one yet."""
    target = Path(raw_path).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"{target} is not a directory")
    dest = target / HELPERS_FILE
    created = not dest.exists()
    if created:
        dest.write_text(_HELPERS_PY, encoding="utf-8")
    return {"file": HELPERS_FILE, "created": created,
            "text": dest.read_text(encoding="utf-8", errors="replace")}


def _write_project_file(raw_path: str, rel: str, text: str) -> dict:
    """Write `rel` back into the project, refusing anything that won't parse.

    Path confinement matches _read_project_file. Python files are compiled
    first: Genesis will not leave a project in a state where `python demo.py`
    dies on a SyntaxError, whether the text came from a form, a behaviour box
    or the helpers editor.
    """
    target = Path(raw_path).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"{target} is not a directory")

    candidate = (target / rel).resolve()
    if candidate != target and target not in candidate.parents:
        raise PermissionError("path escapes the project directory")
    if candidate.is_dir():
        raise ValueError(f"{rel} is a directory")

    if candidate.suffix == ".py":
        try:
            ast.parse(text)
        except SyntaxError as e:
            raise ValueError(f"{e.msg} (line {e.lineno})") from e

    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(text, encoding="utf-8")
    return {"file": rel, "path": str(candidate), "text": text}


# ---------------------------------------------------------------------------
# The structured component model (the interactive Code tab)
# ---------------------------------------------------------------------------

def _component_model(raw_path: str, rel: str) -> dict:
    """Parse one component module into declaration + behaviours + the rest."""
    read = _read_project_file(raw_path, rel)
    model = _ga.parse_component(read["text"])
    model["file"] = rel
    model["text"] = read["text"]
    if model["kind"]:
        decl = model["declaration"] or {}
        model["catalogue"] = _gp.catalogue(
            model["kind"], model["shape"] or "",
            decl.get("callee", ""), decl.get("source", ""),
            (model["backend"] or {}).get("callee", ""),
        )
    else:
        model["catalogue"] = None
    return model


def _apply_edit(raw_path: str, rel: str, mutate) -> dict:
    """Read, transform, validate, write, and hand back the fresh model.

    Every interactive edit goes through here so they all share one contract:
    the file is only written if the transform produced something that parses,
    and the response is always the *re-read* model rather than the client's
    optimistic guess at what it now looks like.
    """
    read = _read_project_file(raw_path, rel)
    new_text = mutate(read["text"])
    _write_project_file(raw_path, rel, new_text)
    return _component_model(raw_path, rel)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_app(dist: Path | None):
    """Assemble the aiohttp app: the SPA, its assets, and the local API.

    Split out of run_genesis so the routes can be exercised without binding
    a port or opening a browser - the API is most of Genesis now, and an
    untestable API is one that quietly rots.
    """
    import aiohttp
    from aiohttp import web

    async def handle_index(request):
        if dist is None:
            return web.Response(text=_MISSING_BUILD_HTML, content_type="text/html")
        return web.FileResponse(dist / "index.html")

    async def handle_browse(request):
        return web.json_response(_list_dir(request.query.get("path")))

    async def handle_init(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        name = (body.get("name") or "").strip()
        folder = (body.get("path") or "").strip()
        namespace = (body.get("namespace") or "demo").strip() or "demo"
        force = bool(body.get("force", False))

        if not name:
            return web.json_response({"error": "name is required"}, status=400)
        if not folder:
            return web.json_response({"error": "path is required"}, status=400)

        target_path = str(Path(folder).expanduser() / name)  # noqa: ASYNC240 - local path math, no I/O
        try:
            target, written = scaffold_project(
                target_path, namespace=namespace, force=force,
            )
        except ScaffoldExistsError as e:
            return web.json_response({"error": str(e), "exists": True}, status=409)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=400)

        return web.json_response({
            "target": str(target), "written": written, "namespace": namespace,
        })

    async def handle_scaffold(request):
        raw_path = request.query.get("path")
        if not raw_path:
            return web.json_response({"error": "path is required"}, status=400)
        try:
            return web.json_response(_read_scaffold(raw_path))
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)

    async def handle_detect(request):
        return web.json_response(_detect_project(request.query.get("path") or ""))

    async def handle_file(request):
        raw_path = request.query.get("path")
        rel = request.query.get("file")
        if not raw_path or not rel:
            return web.json_response(
                {"error": "path and file are required"}, status=400,
            )
        try:
            return web.json_response(_read_project_file(raw_path, rel))
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except PermissionError as e:
            return web.json_response({"error": str(e)}, status=403)
        except (ValueError, OSError) as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_component(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        raw_path = (body.get("path") or "").strip()
        kind = (body.get("kind") or "").strip()
        name = (body.get("name") or "").strip()
        shape = (body.get("shape") or "").strip()
        force = bool(body.get("force", False))

        if not raw_path:
            return web.json_response({"error": "path is required"}, status=400)
        if not name:
            return web.json_response({"error": "name is required"}, status=400)

        try:
            return web.json_response(
                _create_component(raw_path, kind, name, force, shape),
            )
        except ComponentError as e:
            return web.json_response(
                {"error": str(e), "exists": e.exists},
                status=409 if e.exists else 400,
            )
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_write_file(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        raw_path = (body.get("path") or "").strip()
        rel = (body.get("file") or "").strip()
        text = body.get("text")
        if not raw_path or not rel or text is None:
            return web.json_response(
                {"error": "path, file and text are required"}, status=400,
            )
        try:
            return web.json_response(_write_project_file(raw_path, rel, text))
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except PermissionError as e:
            return web.json_response({"error": str(e)}, status=403)
        except (ValueError, OSError) as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_helpers(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        raw_path = (body.get("path") or "").strip()
        if not raw_path:
            return web.json_response({"error": "path is required"}, status=400)
        try:
            return web.json_response(_ensure_helpers(raw_path))
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_model(request):
        raw_path = request.query.get("path")
        rel = request.query.get("file")
        if not raw_path or not rel:
            return web.json_response(
                {"error": "path and file are required"}, status=400,
            )
        try:
            return web.json_response(_component_model(raw_path, rel))
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except PermissionError as e:
            return web.json_response({"error": str(e)}, status=403)
        except (_ga.EditError, ValueError, OSError) as e:
            return web.json_response({"error": str(e)}, status=400)

    def _edit_route(mutate_from_body):
        """Wrap one structured edit as a handler.

        Every edit shares the same envelope - {path, file, ...} in, the
        re-read component model out, EditError as a 400 with the reason the
        rewrite was refused. Keeping that in one place is what stops the
        four edit endpoints from drifting apart.
        """
        async def handler(request):
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "invalid JSON body"}, status=400)
            raw_path = (body.get("path") or "").strip()
            rel = (body.get("file") or "").strip()
            if not raw_path or not rel:
                return web.json_response(
                    {"error": "path and file are required"}, status=400,
                )
            try:
                return web.json_response(
                    _apply_edit(raw_path, rel, mutate_from_body(body)),
                )
            except FileNotFoundError as e:
                return web.json_response({"error": str(e)}, status=404)
            except PermissionError as e:
                return web.json_response({"error": str(e)}, status=403)
            except (_ga.EditError, ValueError, KeyError, OSError) as e:
                return web.json_response({"error": str(e)}, status=400)
        return handler

    handle_declaration = _edit_route(
        lambda b: (
            (lambda text: _ga.edit_backend(text, b["fields"]))
            if b.get("which") == "backend"
            else (lambda text: _ga.edit_declaration(text, b["fields"]))
        ),
    )

    handle_behavior = _edit_route(
        lambda b: lambda text: _ga.upsert_behavior(
            text,
            behavior_id=b.get("behavior_id"),
            scope=b.get("scope", "own"),
            protocol=b["protocol"],
            fn_name=b["fn_name"],
            signature=b.get("signature", ""),
            body=b.get("body", ""),
            args=b.get("args") or [],
            is_async=bool(b.get("is_async", True)),
            indent=bool(b.get("indent", True)),
        ),
    )

    handle_behavior_delete = _edit_route(
        lambda b: lambda text: _ga.delete_behavior(text, b["behavior_id"]),
    )

    handle_engram_shape = _edit_route(
        lambda b: lambda text: _ga.set_engram_shape(
            text, shape=b["shape"], backend=b.get("backend", "in-memory"),
        ),
    )

    handle_axon_source = _edit_route(
        lambda b: lambda text: _ga.set_axon_source(
            text, source=b["source"], form=b.get("form", ""),
        ),
    )

    # -- the Test tab -------------------------------------------------------
    #
    # Two halves that deliberately don't know about each other. "Run" owns the
    # process: one brain.py per project, started and stopped explicitly. A
    # "Connect" panel owns a conversation with one receptor inside whatever is
    # already running. That split is why connecting can't accidentally spawn a
    # second brain, and why stopping the brain leaves every panel intact and
    # simply unable to reach anything.

    async def handle_receptors(request):
        raw_path = request.query.get("path")
        if not raw_path:
            return web.json_response({"error": "path is required"}, status=400)
        try:
            return web.json_response(_read_receptors(raw_path))
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_brain_status(request):
        raw_path = request.query.get("path")
        if not raw_path:
            return web.json_response({"error": "path is required"}, status=400)
        return web.json_response(_gr.status(raw_path))

    async def handle_brain_start(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        raw_path = (body.get("path") or "").strip()
        if not raw_path:
            return web.json_response({"error": "path is required"}, status=400)
        try:
            return web.json_response(
                await _gr.start(raw_path, synapse_url=body.get("synapse_url")),
            )
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except _gr.BrainExited as e:
            # 409, not 500: the spawn worked, the brain refused to stay up.
            # exit_code and output travel as fields as well as in the message
            # so the Test tab can show the traceback in its terminal panel.
            return web.json_response(
                {"error": str(e), "exit_code": e.code, "output": e.output},
                status=409,
            )
        except OSError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_brain_stop(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        raw_path = (body.get("path") or "").strip()
        if not raw_path:
            return web.json_response({"error": "path is required"}, status=400)
        return web.json_response(await _gr.stop(raw_path))

    async def handle_brain_ws(request):
        """The terminal panel: the brain's stdout out, typed lines in.

        A CliReceptor's REPL is a plain ``input()`` loop on stdin, so driving
        it is exactly this - no protocol, no framing, just the bytes a
        terminal would have carried.
        """
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)

        raw_path = request.query.get("path") or ""
        brain = _gr.get(raw_path)
        if brain is None:
            await ws.send_json({"type": "status", "running": False,
                                "text": "No brain is running for this project. "
                                        "Press Run to start one."})
            await ws.close()
            return ws

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()

        def on_chunk(chunk: str) -> None:
            # Called from the pump task; hop back onto the loop thread-safely
            # so a slow socket can never block the child's output.
            loop.call_soon_threadsafe(queue.put_nowait, chunk)

        # Scrollback first, so a panel opened after the banner still shows it.
        if brain.scrollback:
            await ws.send_json({"type": "out", "text": brain.scrollback})
        brain.listeners.add(on_chunk)

        async def pump_out():
            while True:
                chunk = await queue.get()
                if ws.closed:
                    return
                await ws.send_json({"type": "out", "text": chunk})

        pump = asyncio.ensure_future(pump_out())
        try:
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    continue
                if payload.get("type") == "in":
                    brain.write(str(payload.get("text", "")))
                elif payload.get("type") == "eof":
                    brain.close_stdin()
        finally:
            brain.listeners.discard(on_chunk)
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        return ws

    async def handle_receptor_http(request):
        """Same-origin proxy to an HTTP Receptor.

        The browser cannot call the receptor directly: an ApiReceptor sends
        no access-control-allow-origin, so a fetch from the Genesis origin to
        :8000 is blocked before it leaves the tab. Proxying through the server
        that already served the page sidesteps CORS entirely, and has the
        happy side effect that the panel reports transport errors ("nothing is
        listening on :8000") rather than an opaque browser failure.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        raw_path = (body.get("path") or "").strip()
        file = (body.get("file") or "").strip()
        if not raw_path or not file:
            return web.json_response(
                {"error": "path and file are required"}, status=400)

        try:
            entry = _receptor_entry(
                Path(raw_path).expanduser().resolve(), file,  # noqa: ASYNC240 - local FS stat, fast
            )
        except (OSError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=400)
        if entry is None:
            return web.json_response(
                {"error": f"{file} does not declare a Receptor."}, status=404)
        if entry["shape"] not in ("api", "chat"):
            return web.json_response(
                {"error": f"{entry['id']} is a {entry['shape']} Receptor - "
                          f"it has no HTTP endpoint to call."}, status=400)

        method = (body.get("method") or "POST").upper()
        endpoint = body.get("endpoint") or entry["config"].get("path") or "/"
        url = _receptor_base_url(entry) + "/" + str(endpoint).lstrip("/")
        payload = body.get("body")
        timeout = aiohttp.ClientTimeout(total=float(body.get("timeout_s") or 120))

        started = time.monotonic()
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.request(
                    method, url,
                    json=payload if method not in ("GET", "HEAD") else None,
                ) as resp,
            ):
                text = await resp.text()
                elapsed = round((time.monotonic() - started) * 1000)
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
                return web.json_response({
                    "ok": True,
                    "url": url,
                    "status": resp.status,
                    "content_type": resp.headers.get("content-type", ""),
                    "elapsed_ms": elapsed,
                    "text": text,
                    "json": parsed,
                })
        except asyncio.TimeoutError:
            return web.json_response({
                "ok": False, "url": url,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "error": f"No response within {timeout.total:g}s.",
            })
        except aiohttp.ClientError as e:
            return web.json_response({
                "ok": False, "url": url,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "error": f"{type(e).__name__}: {e}. Is the brain running, and "
                         f"is this Receptor mounted on that port?",
            })

    # -- the synapse this project talks to ---------------------------------
    #
    # Genesis does not host the synapse (see _genesis_synapse for why): it
    # probes one, spawns one as a subprocess, and points Prism at it. Every
    # answer here is a fresh probe, so a synapse started from a terminal and
    # one started from this UI look identical.

    async def handle_synapse_status(request):
        return web.json_response(await _gs.probe(
            request.query.get("url") or "",
            request.query.get("namespace") or "",
        ))

    async def handle_synapse_start(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        transport = (body.get("transport") or "dev").strip()
        if transport not in ("dev", "memory"):
            return web.json_response(
                {"error": f"Genesis can only start a dev synapse, not {transport!r}."},
                status=400,
            )
        namespace = (body.get("namespace") or "").strip()
        host = (body.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(body.get("port") or _gs.DEFAULT_SYNAPSE_PORT)
        except (TypeError, ValueError):
            return web.json_response({"error": "port must be a number"}, status=400)

        try:
            return web.json_response(await _gs.start_dev_synapse(
                namespace=namespace, port=port, host=host,
            ))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)

    async def handle_synapse_stop(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        url = (body.get("url") or "").strip()
        namespace = (body.get("namespace") or "").strip()
        if not url or not namespace:
            return web.json_response(
                {"error": "url and namespace are required"}, status=400,
            )
        return web.json_response(await _gs.stop_synapse(url, namespace))

    async def handle_prism(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        try:
            port = int(body.get("port") or _gs.DEFAULT_PRISM_PORT)
        except (TypeError, ValueError):
            return web.json_response({"error": "port must be a number"}, status=400)
        try:
            return web.json_response(await _gs.launch_prism(
                synapse_url=(body.get("url") or "").strip(),
                namespace=(body.get("namespace") or "").strip(),
                port=port,
            ))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)

    async def handle_mark(request):

        # The favicon lives at the bundle root, not under /assets.
        if dist is None or not (dist / "mark.png").is_file():
            return web.Response(status=404)
        return web.FileResponse(dist / "mark.png")

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/mark.png", handle_mark)
    app.router.add_get("/api/browse", handle_browse)
    app.router.add_post("/api/init", handle_init)
    app.router.add_get("/api/scaffold", handle_scaffold)
    app.router.add_get("/api/detect", handle_detect)
    app.router.add_get("/api/file", handle_file)
    app.router.add_post("/api/component", handle_component)
    app.router.add_post("/api/file", handle_write_file)
    app.router.add_post("/api/helpers", handle_helpers)
    app.router.add_get("/api/model", handle_model)
    app.router.add_post("/api/declaration", handle_declaration)
    app.router.add_post("/api/behavior", handle_behavior)
    app.router.add_post("/api/behavior/delete", handle_behavior_delete)
    app.router.add_post("/api/engram-shape", handle_engram_shape)
    app.router.add_post("/api/axon-source", handle_axon_source)
    app.router.add_get("/api/receptors", handle_receptors)
    app.router.add_get("/api/brain", handle_brain_status)
    app.router.add_post("/api/brain/start", handle_brain_start)
    app.router.add_post("/api/brain/stop", handle_brain_stop)
    app.router.add_get("/api/brain/ws", handle_brain_ws)
    app.router.add_post("/api/receptor/http", handle_receptor_http)
    app.router.add_get("/api/synapse", handle_synapse_status)
    app.router.add_post("/api/synapse/start", handle_synapse_start)
    app.router.add_post("/api/synapse/stop", handle_synapse_stop)
    app.router.add_post("/api/prism", handle_prism)
    if dist is not None:
        app.router.add_static("/assets", str(dist / "assets"))
    return app


async def run_genesis(port: int) -> None:
    """Start the aiohttp server that hosts Genesis (SPA + local API)."""
    try:
        from aiohttp import web
    except ImportError:
        click.echo(
            "  aiohttp is required for `cosmo genesis`.\n"
            "  Install it with: pip install aiohttp\n",
            err=True,
        )
        raise SystemExit(1)

    app = build_app(_genesis_dist_dir())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    ui_url = f"http://127.0.0.1:{port}"
    if _HAS_RICH:
        console.print()
        console.print("  [bold cyan]cosmo genesis[/bold cyan]")
        console.print(f"  Genesis:   [underline cyan]{ui_url}[/underline cyan]")
        console.print()
        console.print("  [dim]Ctrl-C to stop[/dim]")
        console.print("  " + "-" * 60)
        console.print()
    else:
        print("\n  cosmo genesis")
        print(f"  Genesis: {ui_url}")
        print("  Ctrl-C to stop\n")

    try:
        webbrowser.open(ui_url)
    except Exception:
        pass

    import asyncio
    import signal as _signal

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for s in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        # Synapses and Prisms Genesis spawned belong to Genesis; leaving
        # them holding ports with no UI to manage them is worse than
        # stopping them.
        _gs.shutdown()
        # Brains too: a brain.py holding an HTTP port with no UI left to
        # drive it is the same problem as an orphaned synapse.
        await _gr.shutdown()
        await runner.cleanup()
        if _HAS_RICH:
            console.print()
            console.print("  [dim]Genesis stopped.[/dim]")
            console.print()
        else:
            print("\n  Genesis stopped.\n")
