"""
cosmo synapse
~~~~~~~~~~~~~
Top-level synapse management commands.

Subcommands
-----------
cosmo synapse start <memory|nats|kafka>   Boot a Synapse and stream Signals to stdout.
cosmo synapse view                        List all running synapse namespaces on a server.
cosmo synapse view --namespace=<ns>       Show info + stream Signals for one namespace.
cosmo synapse stop                        Gracefully stop a namespace on a running server.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal as _signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

try:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

from cosmonapse import Signal

# ---------------------------------------------------------------------------
# Pretty-printing helpers (shared with dev.py)
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

    def _err(text: str) -> None:
        _console.print(text, style="bold red")

else:
    def _print_signal(subject: str, sig: Signal) -> None:
        ts = sig.ts.strftime("%H:%M:%S")
        print(f"  {ts}  {sig.type.value:<14}  {sig.trace_id[4:12]}  "
              f"{(sig.neuron or '—'):<18}  {subject}")

    def _hr() -> None:
        print("  " + "─" * 64)

    def _banner_line(text: str, style: str = "") -> None:
        print(text)

    def _err(text: str) -> None:
        print(text, file=sys.stderr)


# ---------------------------------------------------------------------------
# Local state file for nats/kafka processes
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    base = Path(os.environ.get("COSMONAPSE_STATE_DIR", Path.home() / ".cosmonapse"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "registry.json"


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(state, indent=2))


def _register_state(url: str, namespace: str, transport: str, pid: int) -> None:
    state = _load_state()
    state.setdefault(url, {})[namespace] = {
        "transport": transport,
        "pid": pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state)


def _deregister_state(url: str, namespace: str) -> None:
    state = _load_state()
    if url in state and namespace in state[url]:
        del state[url][namespace]
        if not state[url]:
            del state[url]
        _save_state(state)


# ---------------------------------------------------------------------------
# Management helpers — low-level async TCP calls to DevSynapseServer
# ---------------------------------------------------------------------------

async def _mgmt_send_recv(host: str, port: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Open a management connection, send one op, receive one response, close."""
    reader, writer = await asyncio.open_connection(host, port)
    await reader.readline()  # discard welcome
    writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
    return json.loads(line.decode("utf-8"))


def _parse_cosmo_url(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return (parsed.hostname or "127.0.0.1", parsed.port or 7070)


def _url_scheme(url: str) -> str:
    return url.split("://")[0].lower()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _display_ns_list(namespaces: list[dict[str, Any]], url: str) -> None:
    if not namespaces:
        _banner_line(f"  No namespaces registered on {url}")
        return

    if _HAS_RICH:
        table = Table(show_header=True, header_style="bold cyan",
                      box=None, padding=(0, 2))
        table.add_column("Namespace", style="bold")
        table.add_column("Transport")
        table.add_column("Signals", justify="right")
        table.add_column("Started")
        for ns in namespaces:
            table.add_row(
                ns["namespace"],
                ns["transport"],
                str(ns["signal_count"]),
                ns["started_at"],
            )
        _banner_line("")
        _console.print(table)
        _banner_line("")
    else:
        _banner_line(f"\n  Namespaces on {url}\n")
        _banner_line(f"  {'NAMESPACE':<20}  {'TRANSPORT':<10}  {'SIGNALS':>8}  STARTED")
        _hr()
        for ns in namespaces:
            _banner_line(f"  {ns['namespace']:<20}  {ns['transport']:<10}  "
                         f"{ns['signal_count']:>8}  {ns['started_at']}")
        _banner_line("")


def _display_ns_info(info: dict[str, Any]) -> None:
    _banner_line("")
    _banner_line(f"  Namespace:  {info['namespace']}", "bold cyan" if _HAS_RICH else "")
    _banner_line(f"  Transport:  {info['transport']}")
    _banner_line(f"  Signals:    {info['signal_count']}")
    _banner_line(f"  Clients:    {info.get('client_count', '?')}")
    _banner_line(f"  Started:    {info['started_at']}")
    _banner_line("")


# ---------------------------------------------------------------------------
# `cosmo synapse` group
# ---------------------------------------------------------------------------

@click.group()
def synapse() -> None:
    """Manage Cosmonapse synapse servers."""


# ---------------------------------------------------------------------------
# `cosmo synapse start`
# ---------------------------------------------------------------------------

@synapse.command("start")
@click.argument("transport", type=click.Choice(["memory", "nats", "kafka"]))
@click.option("--namespace", "-n", default="dev", show_default=True,
              help="Namespace to register on the synapse.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host to bind (memory only).")
@click.option("--port", default=7070, show_default=True,
              help="Port to bind (memory only). Use 0 for OS-assigned.")
@click.option("--broker", default=None, metavar="URL",
              help="Broker URL for nats (nats://...) or kafka (kafka://...). "
                   "Defaults: nats://127.0.0.1:4222, localhost:9092.")
@click.option("--quiet", is_flag=True, default=False,
              help="Don't stream Signals to stdout.")
def start(transport: str, namespace: str, host: str, port: int,
          broker: str | None, quiet: bool) -> None:
    """Boot a Synapse and stream every Signal that crosses it.

    TRANSPORT is one of: memory, nats, kafka.

    \b
    Examples:
      cosmo synapse start memory --namespace=dev
      cosmo synapse start nats   --namespace=prod --broker=nats://localhost:4222
      cosmo synapse start kafka  --namespace=prod --broker=localhost:9092
    """
    asyncio.run(_run_start(
        transport=transport, namespace=namespace,
        host=host, port=port, broker=broker, quiet=quiet,
    ))


async def _run_start(
    transport: str, namespace: str,
    host: str, port: int, broker: str | None, quiet: bool,
) -> None:
    if transport == "memory":
        await _start_memory(namespace=namespace, host=host, port=port, quiet=quiet)
    elif transport == "nats":
        await _start_nats(namespace=namespace, broker=broker, quiet=quiet)
    elif transport == "kafka":
        await _start_kafka(namespace=namespace, broker=broker, quiet=quiet)


# -- memory ----------------------------------------------------------------

async def _start_memory(namespace: str, host: str, port: int, quiet: bool) -> None:
    from cosmonapse.synapse.dev import DevSynapseServer

    server = DevSynapseServer(host=host, port=port)
    await server.start()

    # Open a management connection to register the namespace and listen for
    # a remote `mgmt_stop` (which will send us "ns_stopping").
    reader, writer = await asyncio.open_connection(server.host, server.port)
    await reader.readline()  # welcome
    writer.write(
        (json.dumps({"op": "ns_register", "namespace": namespace, "transport": "memory"},
                    separators=(",", ":")) + "\n").encode()
    )
    await writer.drain()
    await reader.readline()  # ns_registered ack

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
    _banner_line("  cosmo synapse start memory", "bold cyan" if _HAS_RICH else "")
    _banner_line(f"  URL:        cosmo://{server.host}:{server.port}",
                 "cyan" if _HAS_RICH else "")
    _banner_line(f"  Namespace:  {namespace}")
    _banner_line("  Transport:  TCP + NDJSON  (single-host dev only)",
                 "dim" if _HAS_RICH else "")
    _banner_line("")
    _banner_line("  Connect a Dendrite or Cortex with:", "dim" if _HAS_RICH else "")
    _banner_line(f"    await Cortex.connect('cosmo://{server.host}:{server.port}', ...)",
                 "dim" if _HAS_RICH else "")
    _banner_line("")
    _banner_line("  Ctrl-C  or  cosmo synapse stop  to stop.", "dim" if _HAS_RICH else "")
    _hr()
    _banner_line("")

    stop_event = asyncio.Event()

    # Listen for ns_stopping from a remote cosmo synapse stop call.
    async def _mgmt_listener() -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
                if msg.get("op") == "ns_stopping" and msg.get("namespace") == namespace:
                    stop_event.set()
                    break
            except Exception:
                continue

    mgmt_task = asyncio.create_task(_mgmt_listener())

    loop = asyncio.get_event_loop()
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        mgmt_task.cancel()
        try:
            await mgmt_task
        except asyncio.CancelledError:
            pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        _banner_line("")
        _banner_line(
            f"  Synapse stopped.  namespace={namespace!r}  signals={signal_count}",
            "dim" if _HAS_RICH else "",
        )
        _banner_line("")
        await server.stop()


# -- nats ------------------------------------------------------------------

async def _start_nats(namespace: str, broker: str | None, quiet: bool) -> None:
    broker = broker or "nats://127.0.0.1:4222"
    from cosmonapse.synapse.nats import NatsSynapse

    synapse_obj = NatsSynapse(url=broker)
    try:
        await synapse_obj.connect()
    except ImportError as exc:
        _err(str(exc))
        raise SystemExit(1)

    _register_state(broker, namespace, "nats", os.getpid())

    signal_count = 0

    async def _handler(sig: Signal) -> None:
        nonlocal signal_count
        signal_count += 1
        if not quiet:
            _print_signal(f"cosmonapse.{namespace}.{sig.type.value}", sig)

    sub = await synapse_obj.subscribe(f"cosmonapse.{namespace}.>", _handler)

    _banner_line("")
    _banner_line("  cosmo synapse start nats", "bold cyan" if _HAS_RICH else "")
    _banner_line(f"  Broker:     {broker}", "cyan" if _HAS_RICH else "")
    _banner_line(f"  Namespace:  {namespace}")
    _banner_line("")
    _banner_line("  Ctrl-C  or  cosmo synapse stop --url={broker} --namespace={namespace}",
                 "dim" if _HAS_RICH else "")
    _hr()
    _banner_line("")

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _deregister_state(broker, namespace)
        await sub.unsubscribe()
        await synapse_obj.close()
        _banner_line("")
        _banner_line(
            f"  NATS synapse stopped.  namespace={namespace!r}  signals={signal_count}",
            "dim" if _HAS_RICH else "",
        )
        _banner_line("")


# -- kafka -----------------------------------------------------------------

async def _start_kafka(namespace: str, broker: str | None, quiet: bool) -> None:
    broker = broker or "localhost:9092"
    # Normalise: strip kafka:// scheme if user supplied it
    broker_addr = broker.replace("kafka://", "")
    from cosmonapse.synapse.kafka import KafkaSynapse

    synapse_obj = KafkaSynapse(bootstrap_servers=broker_addr)
    try:
        await synapse_obj.connect()
    except ImportError as exc:
        _err(str(exc))
        raise SystemExit(1)

    state_key = f"kafka://{broker_addr}"
    _register_state(state_key, namespace, "kafka", os.getpid())

    signal_count = 0

    async def _handler(sig: Signal) -> None:
        nonlocal signal_count
        signal_count += 1
        if not quiet:
            _print_signal(f"cosmonapse.{namespace}.{sig.type.value}", sig)

    sub = await synapse_obj.subscribe(f"cosmonapse.{namespace}.>", _handler)

    _banner_line("")
    _banner_line("  cosmo synapse start kafka", "bold cyan" if _HAS_RICH else "")
    _banner_line(f"  Broker:     {broker_addr}", "cyan" if _HAS_RICH else "")
    _banner_line(f"  Namespace:  {namespace}")
    _banner_line("")
    _banner_line(f"  Ctrl-C  or  cosmo synapse stop --url=kafka://{broker_addr} "
                 f"--namespace={namespace}", "dim" if _HAS_RICH else "")
    _hr()
    _banner_line("")

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _deregister_state(state_key, namespace)
        await sub.unsubscribe()
        await synapse_obj.close()
        _banner_line("")
        _banner_line(
            f"  Kafka synapse stopped.  namespace={namespace!r}  signals={signal_count}",
            "dim" if _HAS_RICH else "",
        )
        _banner_line("")


# ---------------------------------------------------------------------------
# `cosmo synapse view`
# ---------------------------------------------------------------------------

@synapse.command("view")
@click.option("--url", required=True, metavar="URL",
              help="Synapse URL (cosmo://host:port  |  nats://...  |  kafka://...).")
@click.option("--namespace", "-n", default=None, metavar="NS",
              help="Show info + stream Signals for this namespace. "
                   "Omit to list all namespaces.")
def view(url: str, namespace: str | None) -> None:
    """List all namespaces on a synapse, or stream Signals for one namespace.

    \b
    Examples:
      cosmo synapse view --url=cosmo://127.0.0.1:7070
      cosmo synapse view --url=cosmo://127.0.0.1:7070 --namespace=dev
      cosmo synapse view --url=nats://localhost:4222
    """
    asyncio.run(_run_view(url=url, namespace=namespace))


async def _run_view(url: str, namespace: str | None) -> None:
    scheme = _url_scheme(url)

    if scheme == "cosmo":
        host, port = _parse_cosmo_url(url)

        if namespace is None:
            # List all registered namespaces
            try:
                resp = await _mgmt_send_recv(host, port, {"op": "mgmt_list"})
            except (ConnectionRefusedError, OSError):
                _err(f"  Cannot connect to {url}. Is a synapse running there?")
                raise SystemExit(1)
            if resp.get("op") == "mgmt_ns_list":
                _display_ns_list(resp["namespaces"], url)
            else:
                _err(f"  Unexpected response: {resp}")
                raise SystemExit(1)
        else:
            # Show info then stream signals for the namespace
            try:
                info_resp = await _mgmt_send_recv(host, port, {"op": "mgmt_info",
                                                               "namespace": namespace})
            except (ConnectionRefusedError, OSError):
                _err(f"  Cannot connect to {url}. Is a synapse running there?")
                raise SystemExit(1)
            if info_resp.get("op") == "err":
                _err(f"  {info_resp.get('message')}")
                raise SystemExit(1)
            _display_ns_info(info_resp)

            # Live signal stream via raw subscription on a new connection
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except (ConnectionRefusedError, OSError):
                _err(f"  Cannot connect to {url} for signal stream.")
                raise SystemExit(1)

            await reader.readline()  # welcome
            sub_msg = json.dumps({
                "op": "sub",
                "sub_id": "view1",
                "subject": f"cosmonapse.{namespace}.>",
                "queue_group": None,
            }, separators=(",", ":")) + "\n"
            writer.write(sub_msg.encode())
            await writer.drain()

            _banner_line(f"  Streaming  cosmonapse.{namespace}.>  — Ctrl-C to stop",
                         "dim" if _HAS_RICH else "")
            _hr()
            _banner_line("")

            stop_event = asyncio.Event()
            loop = asyncio.get_event_loop()
            for sig in (_signal.SIGINT, _signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop_event.set)
                except NotImplementedError:
                    pass

            async def _stream() -> None:
                while not stop_event.is_set():
                    try:
                        line = await asyncio.wait_for(reader.readline(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if not line:
                        break
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    if msg.get("op") == "msg":
                        try:
                            sig = Signal.decode(msg["frame"])
                            _print_signal(msg.get("subject", "?"), sig)
                        except Exception:
                            pass

            try:
                await asyncio.wait_for(_stream(), timeout=None)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                stop_event.set()
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    elif scheme in ("nats", "kafka"):
        _view_from_state(url, namespace)
    else:
        _err(f"  Unknown URL scheme {scheme!r}. Expected cosmo://, nats://, or kafka://.")
        raise SystemExit(1)


def _view_from_state(url: str, namespace: str | None) -> None:
    # Normalise kafka URL key
    if _url_scheme(url) == "kafka":
        url = f"kafka://{url.replace('kafka://', '')}"

    state = _load_state()
    ns_map: dict[str, Any] = state.get(url, {})

    if not ns_map:
        _banner_line(f"  No namespaces found for {url}")
        _banner_line("  (State is tracked in ~/.cosmonapse/registry.json)")
        return

    if namespace is None:
        ns_list = [
            {"namespace": ns, **info}
            for ns, info in ns_map.items()
        ]
        _display_ns_list(ns_list, url)
    else:
        if namespace not in ns_map:
            _err(f"  Namespace {namespace!r} not found for {url}")
            raise SystemExit(1)
        info = ns_map[namespace]
        _banner_line("")
        _banner_line(f"  Namespace:  {namespace}", "bold cyan" if _HAS_RICH else "")
        _banner_line(f"  Transport:  {info.get('transport', '?')}")
        _banner_line(f"  PID:        {info.get('pid', '?')}")
        _banner_line(f"  Started:    {info.get('started_at', '?')}")
        _banner_line("")


# ---------------------------------------------------------------------------
# `cosmo synapse stop`
# ---------------------------------------------------------------------------

@synapse.command("stop")
@click.option("--url", required=True, metavar="URL",
              help="Synapse URL (cosmo://host:port  |  nats://...  |  kafka://...).")
@click.option("--namespace", "-n", required=True, metavar="NS",
              help="Namespace to stop.")
def stop(url: str, namespace: str) -> None:
    """Gracefully stop a running synapse namespace.

    \b
    Examples:
      cosmo synapse stop --url=cosmo://127.0.0.1:7070 --namespace=dev
      cosmo synapse stop --url=nats://localhost:4222   --namespace=prod
    """
    asyncio.run(_run_stop(url=url, namespace=namespace))


async def _run_stop(url: str, namespace: str) -> None:
    scheme = _url_scheme(url)

    if scheme == "cosmo":
        host, port = _parse_cosmo_url(url)
        try:
            resp = await _mgmt_send_recv(host, port, {"op": "mgmt_stop",
                                                      "namespace": namespace})
        except (ConnectionRefusedError, OSError):
            _err(f"  Cannot connect to {url}. Is a synapse running there?")
            raise SystemExit(1)
        if resp.get("op") == "mgmt_stop_ack":
            _banner_line("")
            _banner_line(
                f"  Stopped namespace {namespace!r} on {url}.",
                "bold green" if _HAS_RICH else "",
            )
            _banner_line("")
        elif resp.get("op") == "err":
            _err(f"  Error: {resp.get('message')}")
            raise SystemExit(1)
        else:
            _err(f"  Unexpected response: {resp}")
            raise SystemExit(1)

    elif scheme in ("nats", "kafka"):
        _stop_from_state(url, namespace)
    else:
        _err(f"  Unknown URL scheme {scheme!r}. Expected cosmo://, nats://, or kafka://.")
        raise SystemExit(1)


def _stop_from_state(url: str, namespace: str) -> None:
    if _url_scheme(url) == "kafka":
        url = f"kafka://{url.replace('kafka://', '')}"

    state = _load_state()
    ns_map = state.get(url, {})

    if namespace not in ns_map:
        _err(f"  Namespace {namespace!r} not found for {url}.")
        _banner_line("  (Is the synapse running? Check ~/.cosmonapse/registry.json)")
        raise SystemExit(1)

    pid = ns_map[namespace].get("pid")
    if pid:
        try:
            os.kill(pid, _signal.SIGTERM)
            _banner_line("")
            _banner_line(
                f"  Sent SIGTERM to PID {pid}  (namespace={namespace!r}  url={url})",
                "bold green" if _HAS_RICH else "",
            )
            _banner_line("")
        except ProcessLookupError:
            _banner_line(f"  Process {pid} is already gone.")
        except PermissionError:
            _err(f"  No permission to send SIGTERM to PID {pid}.")
            raise SystemExit(1)
    else:
        _err(f"  No PID recorded for namespace {namespace!r}.")
        raise SystemExit(1)

    # Clean up state regardless
    _deregister_state(url, namespace)
