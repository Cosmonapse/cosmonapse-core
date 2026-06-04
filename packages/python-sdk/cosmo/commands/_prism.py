"""
cosmo.commands._prism
~~~~~~~~~~~~~~~~~~~~~~~
Prism — the browser visualization for the Doppler.

Architecture
------------
An aiohttp app on a single port (default 7071) serves the Prism single-page
app and a WebSocket bridge:

    GET  /          -> the Prism SPA (index.html)
    GET  /view      -> back-compat redirect to /?<query> (old two-page flow)
    GET  /assets/*  -> the SPA's static JS/CSS bundle
    WS   /ws        -> per-connection Synapse subscriber; broadcasts every
                       Signal on the wildcard subject as one JSON line

Every WebSocket connection opens its own Synapse client so the user can switch
URLs/namespaces from the SPA's form without restarting the server. The client
is closed on WS disconnect.

The frontend is a Vite + React + TypeScript app that lives in
``packages/prism-ui`` and is built to static assets bundled into this wheel at
``cosmo/commands/prism_dist`` (see that package's README). This module no longer
templates HTML — it serves the prebuilt SPA and the ``/ws`` bridge. The bridge
streams one JSON Signal envelope per message; that WS contract is the entire API
between this server and the SPA.
"""

from __future__ import annotations

import asyncio
import json
import signal as _signal
import webbrowser
from importlib.resources import files as _pkg_files
from pathlib import Path

import click

from cosmonapse import Signal, SignalType, discover_signal

from cosmo.commands._shared import _HAS_RICH

if _HAS_RICH:
    from rich.console import Console

    console = Console()


# ---------------------------------------------------------------------------
# Bundled SPA build location
# ---------------------------------------------------------------------------

def _prism_dist_dir() -> Path | None:
    """Locate the bundled Prism SPA build, or None if it was never built.

    The static assets are produced by ``npm run build:into-wheel`` in
    packages/prism-ui and shipped inside this package as ``prism_dist/``.
    Released wheels always contain it; a source checkout only has it after the
    UI has been built.
    """
    try:
        root = Path(str(_pkg_files("cosmo.commands"))) / "prism_dist"
    except (ModuleNotFoundError, TypeError):
        return None
    return root if (root / "index.html").is_file() else None


_MISSING_BUILD_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Prism - not built</title>
<style>body{background:#07080c;color:#e6e7ec;font-family:ui-monospace,Menlo,monospace;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{max-width:560px;padding:32px;border:1px solid rgba(255,255,255,.12);border-radius:14px}
code{color:#22d3ee} h1{font-size:18px;margin:0 0 12px}</style></head>
<body><div class="box">
<h1>Prism UI is not bundled in this install</h1>
<p>The static frontend was not found at <code>cosmo/commands/prism_dist</code>.</p>
<p>Build it from the repo with:</p>
<p><code>cd packages/prism-ui &amp;&amp; npm install &amp;&amp; npm run build:into-wheel</code></p>
<p>then reinstall the SDK (<code>pip install -e .</code>). Released wheels ship
the prebuilt UI, so this only appears for source checkouts that have not built
the UI yet.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Synapse factory (mirrors the one in doppler.py — kept here to avoid an
# import cycle and so this module is fully self-contained).
# ---------------------------------------------------------------------------

def _make_synapse(base_url: str):
    scheme = base_url.split("://")[0].lower()
    if scheme == "cosmo":
        from cosmonapse.synapse.dev import DevSynapse
        return DevSynapse(url=base_url)
    elif scheme == "nats":
        from cosmonapse.synapse.nats import NatsSynapse
        return NatsSynapse(url=base_url)
    elif scheme == "kafka":
        from cosmonapse.synapse.kafka import KafkaSynapse
        broker = base_url.replace("kafka://", "")
        return KafkaSynapse(bootstrap_servers=broker)
    else:
        raise click.ClickException(
            f"Unknown synapse scheme {scheme!r}. "
            "Use cosmo://, nats://, or kafka://."
        )


def _error_envelope(code: str, message: str) -> str:
    """Synthesize a JSON Signal envelope describing a Prism-side error."""
    return json.dumps({
        "v": "1",
        "id": f"evt_prism_{code}",
        "trace_id": "trc_prism_internal",
        "type": "ERROR",
        "neuron": None,
        "ts": "1970-01-01T00:00:00Z",
        "payload": {"code": code, "message": message, "recoverable": False},
        "meta": {"source": "prism"},
    })


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_prism(
    initial_base_url: str | None,
    initial_namespace: str | None,
    port: int,
) -> None:
    """Start the aiohttp server that hosts Prism (SPA + WS bridge)."""
    try:
        from aiohttp import web
    except ImportError:
        click.echo(
            "  aiohttp is required for --prism mode.\n"
            "  Install it with: pip install aiohttp\n",
            err=True,
        )
        raise SystemExit(1)

    dist = _prism_dist_dir()

    # If the CLI was given a synapse URL, send the browser straight to the
    # visualization by pre-seeding the query string the SPA reads on load.
    initial_qs = ""
    if initial_base_url:
        from urllib.parse import urlencode
        initial_qs = "?" + urlencode({
            "url": initial_base_url,
            "namespace": initial_namespace or "dev",
        })

    async def handle_index(request):
        if dist is None:
            return web.Response(text=_MISSING_BUILD_HTML, content_type="text/html")
        # Honour a CLI-seeded target on the bare path only (no query yet) so a
        # reload or manual edit of the query string still works.
        if initial_qs and not request.query_string:
            raise web.HTTPFound("/" + initial_qs)
        return web.FileResponse(dist / "index.html")

    async def handle_view(request):
        # Back-compat with the old two-page flow (/view?url=&namespace=): the
        # SPA now lives at the root and reads the same query params, so just
        # redirect preserving the query string.
        qs = request.query_string
        raise web.HTTPFound("/" + (("?" + qs) if qs else ""))

    async def handle_ws(request):
        """
        Per-connection synapse subscriber.

        Reads ?url=...&namespace=... from the query string, opens its own
        Synapse client, broadcasts a DISCOVER, and forwards every Signal
        on the wildcard subject to the WebSocket as JSON. Tears down both
        the subscription and the synapse when the client disconnects.
        """
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        base_url = request.query.get("url")
        namespace = request.query.get("namespace") or "dev"
        if not base_url:
            await ws.send_str(_error_envelope("no_url", "missing url query param"))
            await ws.close()
            return ws

        try:
            syn = _make_synapse(base_url)
        except click.ClickException as e:
            await ws.send_str(_error_envelope("bad_url", e.format_message()))
            await ws.close()
            return ws

        try:
            await syn.connect()
        except Exception as exc:
            await ws.send_str(_error_envelope("connect_failed", str(exc)))
            await ws.close()
            return ws

        async def on_signal(sig: Signal) -> None:
            if ws.closed:
                return
            try:
                await ws.send_str(sig.model_dump_json())
            except Exception:
                pass

        # Best-effort DISCOVER so existing Dendrites re-publish their
        # REGISTER snapshot and the visualization populates immediately.
        try:
            await syn.publish(
                f"cosmonapse.{namespace}.{SignalType.DISCOVER.value}",
                discover_signal(),
            )
        except Exception:
            pass

        try:
            await syn.subscribe(
                f"cosmonapse.{namespace}.>",
                on_signal,
                queue_group=None,
            )
        except Exception as exc:
            await ws.send_str(_error_envelope("subscribe_failed", str(exc)))
            try:
                await syn.close()
            except Exception:
                pass
            await ws.close()
            return ws

        # Keep the connection open until the client disconnects. We don't
        # read meaningful messages; the WS is one-way (server -> browser).
        try:
            async for _ in ws:
                pass
        finally:
            try:
                await syn.close()
            except Exception:
                pass
        return ws

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/view", handle_view)
    app.router.add_get("/ws", handle_ws)
    if dist is not None:
        app.router.add_static("/assets", str(dist / "assets"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    ui_url = f"http://127.0.0.1:{port}"
    if _HAS_RICH:
        console.print()
        console.print("  [bold cyan]cosmo doppler[/bold cyan]  [dim]--prism[/dim]")
        if initial_base_url:
            console.print(
                f"  Synapse:   [cyan]{initial_base_url}/{initial_namespace or 'dev'}[/cyan]"
            )
        else:
            console.print("  Synapse:   [dim](enter URL in the form)[/dim]")
        console.print(f"  Prism:     [underline cyan]{ui_url}[/underline cyan]")
        console.print()
        console.print("  [dim]Ctrl-C to stop[/dim]")
        console.print("  " + "-" * 60)
        console.print()
    else:
        print("\n  cosmo doppler --prism")
        if initial_base_url:
            print(f"  Synapse: {initial_base_url}/{initial_namespace or 'dev'}")
        else:
            print("  Synapse: (enter URL in the form)")
        print(f"  Prism:   {ui_url}")
        print("  Ctrl-C to stop\n")

    try:
        webbrowser.open(ui_url)
    except Exception:
        pass

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
            console.print("  [dim]Prism stopped.[/dim]")
            console.print()
        else:
            print("\n  Prism stopped.\n")
