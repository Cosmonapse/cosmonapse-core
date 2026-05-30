"""
cosmo.commands._prism
~~~~~~~~~~~~~~~~~~~~~~~
Prism — the browser visualization for the Doppler.

Architecture
------------
Two-stage flow served by an aiohttp app on a single port (default 7071):

    GET  /          → hero page with one form (synapse URL + namespace)
    GET  /view      → animated visualization page
    WS   /ws        → per-connection Synapse subscriber; broadcasts every
                      Signal on the wildcard subject as one JSON line

Every WebSocket connection opens its own Synapse client so the user can switch
URLs/namespaces from the hero form without restarting the server. The client
is closed on WS disconnect.

The HTML/JS templates live in sibling modules so this file stays focused on
the server wiring:

    _prism_hero    HERO_HTML  — landing form
    _prism_view    VIEW_HTML  — animated React visualization
"""

from __future__ import annotations

import asyncio
import json
import signal as _signal
import webbrowser

import click

from cosmonapse import Signal, SignalType, discover_signal

from cosmo.commands._shared import _HAS_RICH
from cosmo.commands._prism_hero import HERO_HTML
from cosmo.commands._prism_view import VIEW_HTML

if _HAS_RICH:
    from rich.console import Console

    console = Console()


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
    """Start the aiohttp server that hosts Prism (hero + visualization)."""
    try:
        from aiohttp import web
    except ImportError:
        click.echo(
            "  aiohttp is required for --prism mode.\n"
            "  Install it with: pip install aiohttp\n",
            err=True,
        )
        raise SystemExit(1)

    # Pre-render hero with any CLI-supplied URL/namespace so the form is
    # pre-filled (the user can still edit before submitting).
    initial_url_safe = (initial_base_url or "").replace('"', "&quot;")
    initial_ns_safe = (initial_namespace or "").replace('"', "&quot;")
    hero_page = (
        HERO_HTML
        .replace("__INITIAL_URL__", initial_url_safe)
        .replace("__INITIAL_NS__", initial_ns_safe)
    )

    async def handle_index(request):
        return web.Response(text=hero_page, content_type="text/html")

    async def handle_view(request):
        ns = request.query.get("namespace") or "dev"
        page = VIEW_HTML.replace("__NAMESPACE__", ns)
        return web.Response(text=page, content_type="text/html")

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
        # read meaningful messages; the WS is one-way (server → browser).
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

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    ui_url = f"http://127.0.0.1:{port}"
    if _HAS_RICH:
        console.print()
        console.print(f"  [bold cyan]cosmo doppler[/bold cyan]  [dim]--prism[/dim]")
        if initial_base_url:
            console.print(
                f"  Synapse:   [cyan]{initial_base_url}/{initial_namespace or 'dev'}[/cyan]"
            )
        else:
            console.print(f"  Synapse:   [dim](enter URL in the form)[/dim]")
        console.print(f"  Prism:     [underline cyan]{ui_url}[/underline cyan]")
        console.print()
        console.print("  [dim]Ctrl-C to stop[/dim]")
        console.print("  " + "─" * 60)
        console.print()
    else:
        print(f"\n  cosmo doppler --prism")
        if initial_base_url:
            print(f"  Synapse: {initial_base_url}/{initial_namespace or 'dev'}")
        else:
            print(f"  Synapse: (enter URL in the form)")
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
