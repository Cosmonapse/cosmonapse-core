"""
cosmo.commands._genesis
~~~~~~~~~~~~~~~~~~~~~~~~
Genesis - the "new brain" wizard: name a project, pick a folder, scaffold it
(same skeleton as `cosmo init`), then look at the result as a draw.io-style
canvas (one Synapse, the Neurons/Effectors/Engram it hosts).

Architecture
------------
An aiohttp app on a single port (default 7072) serves the Genesis single-page
app plus the small local API it needs (a browser can't open a native folder
dialog or run a scaffolder itself):

    GET  /              -> the Genesis SPA (index.html)
    GET  /assets/*       -> the SPA's static JS/CSS bundle
    GET  /api/browse     -> list subdirectories of a path (folder picker)
    POST /api/init       -> run the standard-skeleton scaffold at a path
    GET  /api/scaffold    -> read a scaffolded project back as graph nodes

This mirrors cosmo/commands/_prism.py: same bundling approach
(``genesis_dist``, built by ``packages/genesis-ui``'s
``npm run build:into-wheel``), same "serve the prebuilt SPA, keep the Python
side to a thin local API" shape.
"""

from __future__ import annotations

import re
import webbrowser
from importlib.resources import files as _pkg_files
from pathlib import Path

import click

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


def _extract_id(path: Path, pattern: re.Pattern) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return path.stem
    m = pattern.search(text)
    return m.group(1) if m else path.stem


def _module_nodes(folder: Path, pattern: re.Pattern) -> list[dict]:
    if not folder.is_dir():
        return []
    nodes = []
    for py in sorted(folder.glob("*.py")):
        if py.name == "__init__.py":
            continue
        nodes.append({"id": _extract_id(py, pattern), "file": py.name})
    return nodes


def _read_scaffold(raw_path: str) -> dict:
    """Read a scaffolded project directory back into Genesis's node shape."""
    target = Path(raw_path).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"{target} is not a directory")

    return {
        "project": target.name,
        "path": str(target),
        "synapse": {"id": target.name},
        "neurons": _module_nodes(target / "neurons", _NEURON_ID_RE),
        "effectors": _module_nodes(target / "effector", _EFFECTOR_ID_RE),
        "engrams": _module_nodes(target / "engram", _ENGRAM_ID_RE),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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

    dist = _genesis_dist_dir()

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

        target_path = str(Path(folder).expanduser() / name)
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

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/browse", handle_browse)
    app.router.add_post("/api/init", handle_init)
    app.router.add_get("/api/scaffold", handle_scaffold)
    if dist is not None:
        app.router.add_static("/assets", str(dist / "assets"))

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
        await runner.cleanup()
        if _HAS_RICH:
            console.print()
            console.print("  [dim]Genesis stopped.[/dim]")
            console.print()
        else:
            print("\n  Genesis stopped.\n")
