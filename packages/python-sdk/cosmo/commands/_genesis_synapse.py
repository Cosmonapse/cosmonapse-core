"""
cosmo.commands._genesis_synapse
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The synapse a Genesis project talks to: is one live, start one, stop it,
and point Prism at it.

Why this is a subprocess and not an in-process server
----------------------------------------------------
Genesis is one server that opens many projects, and each project wants its
own synapse on its own port. Hosting `DevSynapseServer` inside the Genesis
event loop would tie every project's broker to one process' lifetime and
make two projects on the same port an unresolvable conflict. So Genesis
spawns exactly what a developer would have typed  -

    python -m cosmo synapse start memory --namespace=NS --port=PORT

- and then treats it the way it treats any other synapse: as something out
there to be probed. That means a synapse Genesis started and one the user
started in a terminal are indistinguishable to the rest of the code, which
is the property that makes the live indicator honest.

Liveness is `mgmt_info` over the dev-synapse wire protocol (see
cosmonapse/synapse/dev.py). A namespace is live when the server on that
port answers for it  -  not when we hold a handle to a process, and not
when a port merely accepts connections.

Only ``cosmo://`` is startable from here. NATS and Kafka are real brokers
with their own deployment story; Genesis can *probe* neither, so its form
offers them greyed out rather than pretending.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

#: Where `cosmo synapse start memory` binds by default, and therefore the
#: port the form offers first.
DEFAULT_SYNAPSE_PORT = 7070
#: Where `cosmo prism` binds by default.
DEFAULT_PRISM_PORT = 7071

#: How long to wait for a spawned synapse to answer for its namespace.
#:
#: `python -m cosmo` imports the whole CLI  -  and through it pydantic,
#: aiohttp and rich  -  before `_start_memory` binds anything. Warm, that is
#: about a second. Cold, on a first run or a cloud-synced project folder or
#: behind a virus scanner that wants to read every file in site-packages, it
#: can be tens of seconds, and none of that is a failure  -  it is just slow.
#: A budget tight enough to trip on it turns a slow start into a red error
#: the user cannot act on, so the default is generous and the machine that
#: needs more can say so.
DEFAULT_START_TIMEOUT = 45.0

#: Synapse child processes this Genesis started, keyed "url|namespace".
#: Only used to reap them  -  never to answer "is it live?", which is
#: always a fresh probe.
_CHILDREN: dict[str, subprocess.Popen] = {}

#: Prism child processes, keyed by port, so a second launch on the same
#: port reuses the server that's already there instead of racing it.
_PRISM: dict[int, subprocess.Popen] = {}


def _key(url: str, namespace: str) -> str:
    return f"{url}|{namespace}"


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

def parse_cosmo_url(url: str) -> tuple[str, int]:
    """Split a cosmo:// URL into (host, port), filling in the defaults."""
    parsed = urlparse(url)
    return (parsed.hostname or "127.0.0.1", parsed.port or DEFAULT_SYNAPSE_PORT)


def scheme_of(url: str) -> str:
    return url.split("://")[0].lower() if "://" in url else ""


def _port_is_open(host: str, port: int, timeout: float = 0.35) -> bool:
    """Is anything accepting connections there? Cheap, and only a hint.

    Used to decide whether to spawn a second Prism on a port, never to
    decide whether a *namespace* is live  -  a port can be occupied by
    something that has never heard of Cosmonapse.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _resolve_timeout(explicit: float | None) -> float:
    """The start budget: what the caller asked for, the env, or the default."""
    if explicit is not None and explicit > 0:
        return explicit
    raw = os.environ.get("COSMO_SYNAPSE_START_TIMEOUT", "").strip()
    if raw:
        with contextlib.suppress(TypeError, ValueError):
            val = float(raw)
            if val > 0:
                return val
    return DEFAULT_START_TIMEOUT


def _drain_stderr(proc: subprocess.Popen) -> str:
    """Everything the child wrote to stderr, or "" if it is still writing.

    `proc.stderr.read()` blocks until the pipe closes, so this is only safe
    once the process is gone. Every caller here kills first and reads second:
    a child being given up on has nothing left to say that is worth waiting
    for, and its traceback is usually the whole answer.
    """
    if proc.stderr is None:
        return ""
    with contextlib.suppress(Exception):
        return (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
    return ""


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

async def _mgmt(host: str, port: int, payload: dict[str, Any],
                timeout: float = 3.0) -> dict[str, Any]:
    """One management round-trip against a DevSynapseServer.

    Mirrors cosmo.commands.synapse._mgmt_send_recv, kept separate so the
    Genesis server never imports the CLI's click commands just to open a
    socket.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout,
    )
    try:
        await asyncio.wait_for(reader.readline(), timeout=timeout)  # welcome
        writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if not line:
        raise ConnectionError("synapse closed the connection")
    return json.loads(line.decode("utf-8"))


def offline(url: str, namespace: str, reason: str | None = None) -> dict:
    """The shape every status answer takes when nothing is there."""
    return {
        "live": False, "url": url, "namespace": namespace,
        "transport": None, "signal_count": None, "started_at": None,
        "client_count": None, "reason": reason, "managed": False,
    }


async def probe(url: str, namespace: str) -> dict:
    """Is `namespace` registered on the synapse at `url` right now?

    Never raises: a probe failing *is* the answer, and the reason is what
    the indicator shows when you ask why it's dark.
    """
    url = (url or "").strip()
    namespace = (namespace or "").strip()
    if not url:
        return offline(url, namespace, "No synapse chosen yet.")
    if not namespace:
        return offline(url, namespace, "This project has no namespace.")

    scheme = scheme_of(url)
    if scheme != "cosmo":
        return offline(
            url, namespace,
            f"Genesis can only see cosmo:// synapses; {scheme or 'that'} "
            "brokers have to be watched from Prism directly.",
        )

    host, port = parse_cosmo_url(url)
    try:
        resp = await _mgmt(host, port, {"op": "mgmt_info", "namespace": namespace})
    except (TimeoutError, OSError):
        # ConnectionRefused, unreachable host and a server that accepts but
        # never answers all mean the same thing to the indicator.
        return offline(url, namespace, f"Nothing is listening on {url}.")
    except Exception as exc:  # malformed reply, decode failure
        return offline(url, namespace, str(exc))

    if resp.get("op") == "err":
        return offline(
            url, namespace,
            f"{url} is up, but namespace {namespace!r} isn't registered on it.",
        )

    return {
        "live": True,
        "url": url,
        "namespace": namespace,
        "transport": resp.get("transport", "memory"),
        "signal_count": resp.get("signal_count"),
        "client_count": resp.get("client_count"),
        "started_at": resp.get("started_at"),
        "reason": None,
        "managed": _key(url, namespace) in _CHILDREN,
    }


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------

def _spawn(args: list[str]) -> subprocess.Popen:
    """Launch a cosmo subcommand detached from Genesis' own stdio.

    Runs through ``sys.executable -m cosmo`` rather than the ``cosmo``
    console script: Genesis may well have been started from a virtualenv
    that isn't on PATH, and the interpreter running us is the one that
    definitely has the SDK importable.

    Deliberately inherits Genesis' cwd rather than running in the project
    folder. Neither the synapse nor Prism reads the project, and ``-m
    cosmo`` resolves against the working directory first  -  a project that
    happens to contain a ``cosmo/`` package or ``cosmo.py`` would shadow the
    SDK and launch something else entirely.
    """
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # New process group, so Ctrl-C in the Genesis terminal doesn't take
        # the synapse down with it.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([sys.executable, "-m", "cosmo", *args], **kwargs)


async def start_dev_synapse(
    namespace: str, port: int, host: str = "127.0.0.1",
    timeout: float | None = None,
) -> dict:
    """Spawn a dev synapse and wait until it actually answers for `namespace`.

    Returning before the namespace is registered would hand the UI a live
    indicator it then has to walk back, so this polls the same probe the
    indicator uses and only returns once that says yes.
    """
    namespace = (namespace or "").strip()
    if not namespace:
        raise ValueError("namespace is required")
    if not (0 < port < 65536):
        raise ValueError(f"{port} is not a usable port")

    timeout = _resolve_timeout(timeout)
    url = f"cosmo://{host}:{port}"

    # Already serving this namespace? Then there is nothing to start, and
    # spawning would only lose a port race with the server that won it.
    existing = await probe(url, namespace)
    if existing["live"]:
        return existing

    if _port_is_open(host, port):
        raise RuntimeError(
            f"Port {port} is already in use by something that isn't serving "
            f"namespace {namespace!r}. Pick another port, or stop what's there."
        )

    proc = _spawn([
        "synapse", "start", "memory",
        f"--namespace={namespace}", f"--host={host}", f"--port={port}",
        "--quiet",
    ])
    _CHILDREN[_key(url, namespace)] = proc

    # Keep the last probe: its `reason` is the difference between "the port
    # never opened" and "the port opened and answered for someone else", and
    # those have different fixes. Throwing it away is what makes a timeout
    # here unactionable.
    last = offline(url, namespace, "the synapse had not started yet")

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if proc.poll() is not None:
            _CHILDREN.pop(_key(url, namespace), None)
            err = _drain_stderr(proc)
            raise RuntimeError(
                f"The synapse exited immediately (exit code {proc.returncode})."
                f"{(' ' + err) if err else ''}"
            )
        last = await probe(url, namespace)
        if last["live"]:
            last["managed"] = True
            return last
        await asyncio.sleep(0.25)

    # Timed out. Read the port *before* killing the child, or the answer is
    # always "nothing there" - we just closed it.
    port_open = _port_is_open(host, port)
    stop_child(url, namespace)
    err = _drain_stderr(proc)

    if port_open:
        hint = (
            f"Something on port {port} is answering, but it is not serving "
            f"{namespace!r}  -  most likely another synapse, or a leftover "
            f"one from an earlier run. Stop it, or pick another port."
        )
    else:
        hint = (
            f"Nothing ever bound port {port}. `python -m cosmo` loads the "
            f"whole SDK before it binds, so a cold first run can take longer "
            f"than {timeout:.0f}s; try again now that the caches are warm, or "
            f"raise COSMO_SYNAPSE_START_TIMEOUT. If it never binds, run the "
            f"same command by hand to see why:\n"
            f"    python -m cosmo synapse start memory "
            f"--namespace={namespace} --host={host} --port={port}"
        )

    raise RuntimeError(
        f"Started a synapse on {url} but namespace {namespace!r} never "
        f"registered within {timeout:.0f}s. {hint}"
        f"{(chr(10) + err) if err else ''}"
    )


def managed_urls() -> dict[str, str]:
    """``{namespace: url}`` for the synapses this Genesis started and still owns.

    Used by ``_genesis_run`` to hand a spawned ``brain.py`` the SYNAPSE_URL of
    the synapse the user just started from the same window. Only *managed*
    children are offered: a synapse Genesis merely probed belongs to someone
    else, and silently binding a brain to it would be a surprise.
    """
    out: dict[str, str] = {}
    for key, proc in _CHILDREN.items():
        if proc.poll() is not None:
            continue
        url, _, namespace = key.rpartition("|")
        out.setdefault(namespace, url)
    return out


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------

def stop_child(url: str, namespace: str) -> bool:
    """Terminate the child Genesis spawned for this url+namespace, if any."""
    proc = _CHILDREN.pop(_key(url, namespace), None)
    if proc is None:
        return False
    with contextlib.suppress(Exception):
        proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    return True


async def stop_synapse(url: str, namespace: str) -> dict:
    """Ask the namespace to stop, then reap our child if we own one.

    The graceful path is `mgmt_stop`, which works whoever started the
    synapse. Killing the process is the fallback for a server that has
    stopped answering, and only ever applies to a process we spawned.
    """
    acked = False
    if scheme_of(url) == "cosmo":
        host, port = parse_cosmo_url(url)
        with contextlib.suppress(Exception):
            resp = await _mgmt(host, port, {"op": "mgmt_stop", "namespace": namespace})
            acked = resp.get("op") == "mgmt_stop_ack"

    killed = stop_child(url, namespace)
    status = await probe(url, namespace)
    status["stopped"] = acked or killed
    return status


def shutdown() -> None:
    """Reap every process Genesis spawned. Called when Genesis exits.

    A synapse outliving the UI that started it would sit on its port with
    nothing left to manage it, so Genesis owns its children end to end.
    """
    for proc in list(_CHILDREN.values()) + list(_PRISM.values()):
        with contextlib.suppress(Exception):
            proc.terminate()
    for proc in list(_CHILDREN.values()) + list(_PRISM.values()):
        with contextlib.suppress(Exception):
            proc.wait(timeout=3)
    _CHILDREN.clear()
    _PRISM.clear()


# ---------------------------------------------------------------------------
# Prism
# ---------------------------------------------------------------------------

def prism_url(port: int, synapse_url: str, namespace: str) -> str:
    qs = urlencode({"url": synapse_url, "namespace": namespace})
    return f"http://127.0.0.1:{port}/?{qs}"


async def launch_prism(
    synapse_url: str, namespace: str, port: int = DEFAULT_PRISM_PORT,
    timeout: float = 15.0,
) -> dict:
    """Open Prism on this synapse + namespace, starting a server if needed.

    Prism reads its target off the query string, so a Prism already running
    on the requested port can simply be re-pointed by opening a new URL -
    no second process, no port conflict. That's the common case when you
    flip between two projects.
    """
    synapse_url = (synapse_url or "").strip()
    namespace = (namespace or "").strip()
    if not synapse_url:
        raise ValueError("a synapse url is required")
    if not (0 < port < 65536):
        raise ValueError(f"{port} is not a usable port")

    target = prism_url(port, synapse_url, namespace)

    if _port_is_open("127.0.0.1", port):
        return {"url": target, "port": port, "started": False,
                "reused": True, "namespace": namespace, "synapse_url": synapse_url}

    # No --url/--namespace here on purpose: those make _prism.py redirect the
    # bare path to its own seeded query string, which would fight the query
    # string we hand the browser. The SPA reads the target from the URL.
    proc = _spawn(["prism", f"--port={port}"])
    _PRISM[port] = proc

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if proc.poll() is not None:
            _PRISM.pop(port, None)
            err = _drain_stderr(proc)
            raise RuntimeError(
                f"Prism exited immediately (exit code {proc.returncode})."
                f"{(' ' + err) if err else ''}"
            )
        if _port_is_open("127.0.0.1", port):
            return {"url": target, "port": port, "started": True,
                    "reused": False, "namespace": namespace,
                    "synapse_url": synapse_url}
        await asyncio.sleep(0.25)

    with contextlib.suppress(Exception):
        proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    _PRISM.pop(port, None)
    err = _drain_stderr(proc)
    raise RuntimeError(
        f"Prism didn't come up on port {port} within {timeout:.0f}s."
        f"{(chr(10) + err) if err else ''}"
    )


# ---------------------------------------------------------------------------
# Project namespace
# ---------------------------------------------------------------------------

def read_namespace(target: Path) -> str | None:
    """The namespace this project was scaffolded with.

    Read out of config.py's ``NAMESPACE = "..."`` rather than asked of the
    user: the project already decided this at `cosmo init` time, and a
    Genesis form that let you retype it would just be a way to connect to
    the wrong namespace. Returns None when there's nothing to read, which
    the form shows as "unset" instead of guessing "demo".
    """
    import re

    config = target / "config.py"
    if not config.is_file():
        return None
    try:
        text = config.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(r'^\s*NAMESPACE\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return m.group(1) if m else None
