"""
cosmo dev
~~~~~~~~~
Local-development helpers.

Subcommands
-----------
cosmo dev synapse   [Deprecated] Use `cosmo synapse start memory` instead.
cosmo dev watch     (Reserved for the legacy in-process doppler view.)
"""

from __future__ import annotations

import asyncio
import signal as _signal
import sys

import click

try:
    from rich.console import Console
    from rich.text import Text
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

from cosmonapse import Signal
from cosmonapse.synapse.dev import DevSynapseServer


# ---------------------------------------------------------------------------
# Pretty-printing helpers
# ---------------------------------------------------------------------------

_TYPE_COLOURS: dict[str, str] = {
    "TASK": "cyan",
    "AGENT_OUTPUT": "green",
    "FINAL": "bold green",
    "ERROR": "bold red",
    "CLARIFICATION": "yellow",
    "REGISTER": "blue",
    "DEREGISTER": "blue",
    "HEARTBEAT": "dim blue",
    "TASK_OFFER": "magenta",
    "BID": "magenta",
    "TASK_AWARDED": "bold magenta",
    "TASK_DECLINED": "dim magenta",
    "THOUGHT_DELTA": "dim white",
    "PLAN": "white",
    "TOOL_CALL": "bright_white",
    "TOOL_RESULT": "bright_white",
    "MEMORY_APPEND": "bright_cyan",
    "ESCALATION": "bold yellow",
    "CONSENSUS": "bold cyan",
    "CONTEXT_SYNC": "cyan",
    "CRITIQUE": "yellow",
}


if _HAS_RICH:
    _console = Console()

    def _print_signal(subject: str, sig: Signal) -> None:
        colour = _TYPE_COLOURS.get(sig.type.value, "white")
        ts = sig.ts.strftime("%H:%M:%S.%f")[:-3]
        neuron = sig.neuron or "—"
        trace = sig.trace_id[4:12]
        t = Text()
        t.append(f"  {ts}  ", style="dim")
        t.append(f"{sig.type.value:<14}", style=colour)
        t.append(f"  {trace}  ", style="dim")
        t.append(f"{neuron:<18}", style="italic")
        t.append(f"  {subject}", style="dim")
        _console.print(t)

    def _hr() -> None:
        _console.print("  " + "─" * 64)

    def _banner_line(text: str, style: str = "") -> None:
        _console.print(text, style=style)
else:
    def _print_signal(subject: str, sig: Signal) -> None:
        ts = sig.ts.strftime("%H:%M:%S")
        print(f"  {ts}  {sig.type.value:<14}  {sig.trace_id[4:12]}  "
              f"{(sig.neuron or '—'):<18}  {subject}")

    def _hr() -> None:
        print("  " + "─" * 64)

    def _banner_line(text: str, style: str = "") -> None:
        print(text)


# ---------------------------------------------------------------------------
# `cosmo dev` group
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.pass_context
def dev(ctx: click.Context) -> None:
    """Local-development helpers (legacy — see `cosmo synapse`)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# `cosmo dev synapse`  [deprecated alias for cosmo synapse start memory]
# ---------------------------------------------------------------------------

@dev.command("synapse", deprecated=True,
             help="[Deprecated] Use `cosmo synapse start memory` instead.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host to bind the Synapse server on.")
@click.option("--port", default=7070, show_default=True,
              help="Port to bind. Use 0 for an OS-assigned port.")
@click.option("--namespace", default="dev", show_default=True,
              help="Namespace to register.")
@click.option("--quiet", is_flag=True, default=False,
              help="Don't stream Signals to stdout (just run the server).")
def dev_synapse(host: str, port: int, namespace: str, quiet: bool) -> None:
    """[Deprecated] Boot a local Synapse server (use `cosmo synapse start memory`)."""
    click.echo(
        "  Warning: cosmo dev synapse is deprecated.\n"
        "  Use: cosmo synapse start memory --namespace=" + namespace + "\n",
        err=True,
    )
    asyncio.run(_run_synapse(host=host, port=port, namespace=namespace, quiet=quiet))


async def _run_synapse(host: str, port: int, quiet: bool, namespace: str = "dev") -> None:
    server = DevSynapseServer(host=host, port=port)
    await server.start()

    signal_count = 0

    if not quiet:
        def _observer(subject: str, frame: str) -> None:
            nonlocal signal_count
            signal_count += 1
            try:
                sig = Signal.decode(frame)
            except Exception:
                return
            _print_signal(subject, sig)
        server.on_signal = _observer

    _banner_line("")
    _banner_line("  cosmonapse  dev synapse  [deprecated]", "bold cyan" if _HAS_RICH else "")
    _banner_line(f"  URL:        cosmo://{server.host}:{server.port}",
                 "cyan" if _HAS_RICH else "")
    _banner_line("  Synapse:  TCP + NDJSON  (single-host dev only)",
                 "dim" if _HAS_RICH else "")
    _banner_line("")
    _banner_line("  Connect a Dendrite or Cortex with:", "dim" if _HAS_RICH else "")
    _banner_line(f"    await Cortex.connect('cosmo://{server.host}:{server.port}', "
                 f"registry_store=...)",
                 "dim" if _HAS_RICH else "")
    _banner_line("")
    _banner_line("  Ctrl-C to stop", "dim" if _HAS_RICH else "")
    _hr()
    _banner_line("")

    stop_event = asyncio.Event()

    def _on_sig(*_):
        stop_event.set()

    loop = asyncio.get_event_loop()
    for s in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _on_sig)
        except NotImplementedError:
            pass  # Windows

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _banner_line("")
        _banner_line(f"  Synapse stopped. {signal_count} signals observed.",
                     "dim" if _HAS_RICH else "")
        _banner_line("")
        await server.stop()
