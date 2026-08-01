"""
cosmo prism
~~~~~~~~~~~
Attach a read-only observer to a running Synapse and visualize its Signals  - 
in **Prism**, the live browser view, or as a plain stream in your terminal.

Usage
-----
    cosmo prism                                                 # opens Prism, enter URL in the form
    cosmo prism --port=8080
    cosmo prism --url=cosmo://127.0.0.1:7070 -n dev             # skip the form
    cosmo prism --tail --url=cosmo://127.0.0.1:7070 -n dev      # stream to stdout instead
    cosmo prism --tail --url=cosmo://127.0.0.1:7070 -n dev --type TASK
    cosmo prism --tail --url=cosmo://127.0.0.1:7070 -n dev --json

Synapse URL + namespace
-----------------------
    The URL identifies the transport endpoint; the namespace is a separate
    flag, matching the SDK (`connect_synapse(url)` + `Dendrite(namespace=...)`)
    and the `cosmo synapse` commands.

    cosmo://127.0.0.1:7070   → DevSynapse (TCP+NDJSON)
    nats://localhost:4222    → NatsSynapse
    kafka://localhost:9092   → KafkaSynapse

Legacy combined form
--------------------
    The older `--synapse=<scheme>://<host>:<port>/<namespace>` form, which
    encodes the namespace in the URL path, is still accepted for back-compat.
    When both a path namespace and an explicit --namespace are given,
    --namespace wins.
"""

from __future__ import annotations

import asyncio
import json
import signal as _signal
from typing import Optional
from urllib.parse import urlparse

import click

from cosmonapse import Signal, SignalType, discover_signal

# The signal-type colour map is shared across the CLI (see _shared.py).
from cosmo.commands._shared import _HAS_RICH, _TYPE_COLOURS

# The Prism browser visualization (hero + animated view + WS bridge) lives in
# its own module so this CLI file stays small.
from cosmo.commands._prism import run_prism as _run_prism

# --tail renders signals with its own payload-aware formatter, so it keeps a
# Console + Text handle here when rich is available.
if _HAS_RICH:
    from rich.console import Console
    from rich.text import Text

    console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_url(raw: str) -> tuple[str, str | None]:
    """
    Split a synapse URL into (base_url, path_namespace_or_None).

    cosmo://127.0.0.1:7070/dev  →  ("cosmo://127.0.0.1:7070", "dev")
    nats://localhost:4222/prod   →  ("nats://localhost:4222",  "prod")
    cosmo://127.0.0.1:7070       →  ("cosmo://127.0.0.1:7070", None)
    """
    parsed = urlparse(raw)
    path_ns = parsed.path.lstrip("/") or None
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return base_url, path_ns


def _resolve_target(
    synapse_arg: str | None,
    url: str | None,
    namespace: str | None,
    *,
    required: bool = True,
) -> tuple[str | None, str | None]:
    """
    Work out the (base_url, namespace) the observer should attach to.

    Accepts either the modern ``--url`` + ``--namespace`` form (matching
    ``cosmo synapse``) or the legacy ``--synapse=<url>/<namespace>`` form that
    encodes the namespace in the URL path. When both a path namespace and an
    explicit ``--namespace`` are supplied, ``--namespace`` wins.

    When ``required`` is False (browser mode) the URL may be omitted  -  the
    user will enter it through the browser form.
    """
    raw = url or synapse_arg
    if raw is None:
        if not required:
            return None, namespace
        raise click.UsageError(
            "Provide a synapse URL with --url (and optionally --namespace), "
            "e.g. --url=cosmo://127.0.0.1:7070 --namespace=dev"
        )
    if url and synapse_arg:
        raise click.UsageError("Use either --url or --synapse, not both.")

    base_url, path_ns = _split_url(raw)
    resolved_ns = namespace or path_ns or "dev"
    return base_url, resolved_ns


def _make_synapse(base_url: str):
    """Return the appropriate Synapse instance for the given URL scheme."""
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


def _render_signal(subject: str, sig: Signal, show_payload: bool = False) -> None:
    if _HAS_RICH:
        colour = _TYPE_COLOURS.get(sig.type.value, "white")
        ts = sig.ts.strftime("%H:%M:%S.%f")[:-3]
        neuron = (sig.directed.id if sig.directed else None) or " - "
        trace = sig.trace_id[4:12]
        t = Text()
        t.append(f"  {ts}  ", style="dim")
        t.append(f"{sig.type.value:<18}", style=colour)
        t.append(f"  {trace}  ", style="dim")
        t.append(f"{neuron}", style="italic")
        if show_payload and sig.payload:
            payload_str = json.dumps(sig.payload, default=str)
            if len(payload_str) > 80:
                payload_str = payload_str[:77] + "…"
            t.append(f"\n    {payload_str}", style="dim")
        console.print(t)
    else:
        ts = sig.ts.strftime("%H:%M:%S")
        print(f"  {ts}  {sig.type.value:<18}  {sig.trace_id[4:12]}  {(sig.directed.id if sig.directed else None) or ' - '}")


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

_TARGET_OPTIONS = [
    click.option(
        "--url", "url", default=None, metavar="URL",
        help="Synapse URL, e.g. cosmo://127.0.0.1:7070  (use with --namespace).",
    ),
    click.option("--namespace", "-n", default=None, metavar="NS",
                 help="Namespace to observe. Defaults to 'dev'."),
    click.option(
        "--synapse", "synapse_arg", default=None, metavar="URL[/NAMESPACE]",
        help="Legacy combined form, e.g. cosmo://127.0.0.1:7070/dev. "
             "Prefer --url + --namespace.",
    ),
    click.option("--port", default=7071, show_default=True,
                 help="Local port for the Prism server (browser mode)."),
    click.option("--tail", "tail", is_flag=True, default=False,
                 help="Stream Signals to stdout instead of opening the browser view."),
    click.option(
        "--type", "filter_types", multiple=True,
        type=click.Choice([t.value for t in SignalType], case_sensitive=False),
        help="Filter to specific signal types (repeatable, --tail only).",
    ),
    click.option("--trace", default=None,
                 help="Filter to a specific trace_id (--tail only)."),
    click.option("--neuron", default=None,
                 help="Filter to a specific neuron ID (--tail only)."),
    click.option("--json", "output_json", is_flag=True,
                 help="Output one JSON object per line (--tail only)."),
    click.option("--payload", is_flag=True,
                 help="Show payload preview alongside each signal (--tail only)."),
    # Accepted and ignored: in the old `cosmo doppler` this flag selected the
    # browser view, which is now the default. Kept so existing scripts and the
    # deprecated `cosmo doppler` alias keep working.
    click.option("--prism", "show_prism", is_flag=True, default=False, hidden=True),
]


def _target_options(fn):
    for opt in reversed(_TARGET_OPTIONS):
        fn = opt(fn)
    return fn


def _run(
    url: Optional[str],
    namespace: Optional[str],
    synapse_arg: Optional[str],
    port: int,
    tail: bool,
    filter_types: tuple[str, ...],
    trace: Optional[str],
    neuron: Optional[str],
    output_json: bool,
    payload: bool,
    show_prism: bool,
) -> None:
    if tail:
        base_url, namespace = _resolve_target(synapse_arg, url, namespace)
        asyncio.run(_run_cli(
            base_url=base_url,
            namespace=namespace,
            filter_types=set(filter_types),
            trace=trace,
            neuron_filter=neuron,
            output_json=output_json,
            show_payload=payload,
        ))
    else:
        base_url, namespace = _resolve_target(
            synapse_arg, url, namespace, required=False,
        )
        asyncio.run(_run_prism(
            initial_base_url=base_url, initial_namespace=namespace, port=port,
        ))


@click.command()
@_target_options
def prism(**kwargs) -> None:
    """Open Prism, the live browser view onto a Synapse namespace.

    \b
    Launch Prism (browser visualization):
      cosmo prism
      cosmo prism --port=8080
      cosmo prism --url=cosmo://127.0.0.1:7070 -n dev

    Stream to stdout instead:
      cosmo prism --tail --url=cosmo://127.0.0.1:7070 --namespace=dev

    Filter by type:
      cosmo prism --tail --url=cosmo://127.0.0.1:7070 -n dev --type TASK --type ERROR

    Legacy combined form (still supported):
      cosmo prism --synapse=cosmo://127.0.0.1:7070/dev
    """
    _run(**kwargs)


@click.command("doppler", hidden=True)
@_target_options
def doppler(**kwargs) -> None:
    """Deprecated alias for `cosmo prism`."""
    click.echo(
        "  Warning: `cosmo doppler` is deprecated and will be removed in a "
        "future release. Use `cosmo prism` (add --tail for the stdout stream).",
        err=True,
    )
    # Preserve the old defaults exactly: bare `cosmo doppler` streamed to
    # stdout and only `--prism` opened the browser. `cosmo prism` inverts
    # that, so the alias re-inverts it for callers that never migrated.
    if not kwargs["tail"] and not kwargs["show_prism"]:
        kwargs["tail"] = True
    _run(**kwargs)


# ---------------------------------------------------------------------------
# CLI mode
# ---------------------------------------------------------------------------

async def _run_cli(
    base_url: str,
    namespace: str,
    filter_types: set[str],
    trace: str | None,
    neuron_filter: str | None,
    output_json: bool,
    show_payload: bool,
) -> None:
    try:
        syn = _make_synapse(base_url)
    except click.ClickException as e:
        click.echo(f"  Error: {e.format_message()}", err=True)
        raise SystemExit(1)

    try:
        await syn.connect()
    except ImportError as e:
        click.echo(f"  {e}", err=True)
        raise SystemExit(1)
    except (ConnectionRefusedError, OSError) as e:
        click.echo(f"  Cannot connect to {base_url}: {e}", err=True)
        raise SystemExit(1)

    if not output_json:
        if _HAS_RICH:
            console.print()
            console.print(f"  [bold cyan]cosmo prism[/bold cyan] [dim]--tail[/dim]  "
                          f"[cyan]{base_url}[/cyan][dim]/{namespace}[/dim]")
            if filter_types:
                console.print(f"  Filtering: {', '.join(sorted(filter_types))}")
            if trace:
                console.print(f"  Trace:  [dim]{trace}[/dim]")
            if neuron_filter:
                console.print(f"  Neuron: [italic]{neuron_filter}[/italic]")
            console.print()
            console.print("  [dim]Observing  -  Ctrl-C to detach[/dim]")
            console.print("  " + "─" * 60)
            console.print()
        else:
            print(f"\n  cosmo prism --tail  {base_url}/{namespace}")
            print("  Observing  -  Ctrl-C to detach")
            print("  " + "─" * 60 + "\n")

    signal_count = 0

    async def handle(sig: Signal) -> None:
        nonlocal signal_count
        if filter_types and sig.type.value not in filter_types:
            return
        if trace and sig.trace_id != trace:
            return
        if neuron_filter and (not sig.directed or sig.directed.id != neuron_filter):
            return
        signal_count += 1
        if output_json:
            print(sig.model_dump_json(), flush=True)
        else:
            _render_signal(f"cosmonapse.{namespace}.{sig.type.value}", sig,
                           show_payload=show_payload)

    subject = f"cosmonapse.{namespace}.>"
    # Broadcast DISCOVER once before the wildcard subscribe so every
    # Dendrite with attached Axons replies with a REGISTER snapshot  - 
    # the observer immediately sees the current namespace state instead
    # of waiting for the next heartbeat tick.
    try:
        await syn.publish(
            f"cosmonapse.{namespace}.{SignalType.DISCOVER.value}",
            discover_signal(),
        )
    except Exception as exc:
        # The DISCOVER probe is best-effort; a backend that doesn't support
        # publish (or transient failure) shouldn't block the subscribe path.
        if not output_json:
            click.echo(f"  (DISCOVER probe skipped: {exc})", err=True)
    await syn.subscribe(subject, handle, queue_group=None)

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
        await syn.close()
        if not output_json:
            if _HAS_RICH:
                console.print()
                console.print(f"  [dim]Detached.  {signal_count} signals observed.[/dim]")
                console.print()
            else:
                print(f"\n  Detached.  {signal_count} signals observed.\n")

