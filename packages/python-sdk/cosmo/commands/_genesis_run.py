"""
cosmo.commands._genesis_run
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Running a project's brain, so the Test tab has something to talk to.

Why a subprocess
----------------
Same reason the synapse is one (see ``_genesis_synapse``), plus a stronger
one: ``brain.py`` is *the user's code*. Importing it into the Genesis server
would run arbitrary project code in the process that is supposed to survive
it, share its event loop, and leak its imports into every other project the
same Genesis has open. So Genesis spawns exactly what a developer would have
typed - ``python -u brain.py`` - and talks to it the way anything else would.

The one difference from the synapse spawn: **cwd is the project**. It has to
be, because ``brain.py`` does ``from neurons import hello``, which only
resolves from the project root. That is safe here precisely because this is
``python <script>`` and not ``python -m cosmo``: there is no module search
for a name a project directory could shadow.

``-u`` matters. The child's stdout is a pipe, so Python would block-buffer it
and a CliReceptor's prompt - written without a newline - would never arrive.
Unbuffered mode is what makes the REPL feel live over a socket.

No PTY
------
A pty would give nicer line editing, but ``pty`` is Unix-only and Genesis
runs on Windows too. It isn't needed: ``CliReceptor.repl()`` reads with a
plain ``input()`` in an executor and never checks ``isatty``, so a pipe
drives it perfectly well.

One brain per project
---------------------
Keyed by resolved project path. Starting a brain that is already running is
idempotent and returns the running one, which keeps the button honest when
two tabs are open on the same project.

Reporting a brain that died on the way up
-----------------------------------------
``create_subprocess_exec`` returning is not the same as a brain running: an
``ImportError``, or a ``brain.py`` from an older skeleton that has ``build_*``
helpers but no ``if __name__ == "__main__"`` block, exits in milliseconds.
Returning ``running: True`` for that hands the UI a green light it then has to
walk back, and the traceback only ever exists in the WebSocket scrollback -
which nobody is attached to yet. So ``start`` holds the spawn for
``_FAST_EXIT_GRACE_S``, and a brain that is already gone by then is raised as
``BrainExited`` with its exit code and whatever it printed. Same shape as
``_genesis_synapse.start_dev_synapse``, for the same reason.

Which synapse the brain talks to
--------------------------------
The scaffold's ``config.py`` reads ``SYNAPSE_URL`` from the environment and
falls back to an in-process ``MemorySynapse``. Inheriting Genesis's env
verbatim therefore means the Run button boots a brain on a private bus even
when the synapse pill next to it is green - invisible to that synapse, and to
Prism. So ``start`` passes the URL of the Genesis-managed synapse serving this
project's namespace, unless the caller named one explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cosmo.commands import _genesis_synapse as _gs

#: How much of the child's output to keep, so a client connecting late still
#: sees the banner and prompt it missed rather than an empty screen.
SCROLLBACK_CHARS = 60_000

#: Grace period between asking a brain to stop and killing it.
_TERM_GRACE_S = 3.0

#: How long ``start`` waits before believing a brain is actually up. Long
#: enough for an import-time failure to unwind, short enough that the Run
#: button still feels instant.
_FAST_EXIT_GRACE_S = 1.0


class BrainExited(RuntimeError):
    """``brain.py`` was spawned but exited before it could serve anything."""

    def __init__(self, code: int, output: str) -> None:
        self.code = code
        # _read_forever annotates the scrollback with its own "[genesis] ..."
        # lines. They are always present by the time a failure is reported, so
        # counting them as child output would suppress the hint below on
        # exactly the case it exists for: a brain that printed nothing.
        self.output = "\n".join(
            line for line in output.strip().splitlines()
            if not line.lstrip().startswith("[genesis]")
        ).strip()
        detail = (
            f" It printed:\n{self.output}" if self.output else
            " It printed nothing, which usually means brain.py has no entry "
            "point: a module of build_* helpers with no `if __name__ == "
            '"__main__"` block imports its parts and returns without starting '
            "anything. `cosmo init` writes that block - a project scaffolded "
            "before it did needs it added."
        )
        super().__init__(f"brain.py exited immediately with code {code}.{detail}")


class Brain:
    """One running ``brain.py``, plus everyone watching its output."""

    def __init__(self, project: Path, proc: asyncio.subprocess.Process,
                 synapse_url: str = "") -> None:
        self.project = project
        self.proc = proc
        #: "" when the brain is running on its own in-process MemorySynapse.
        #: Reported so the Test tab can say which bus it is talking to rather
        #: than leaving the user to guess from the synapse pill.
        self.synapse_url = synapse_url
        self.started_at = time.time()
        self.scrollback = ""
        #: Called with each chunk of child output. WebSocket clients register
        #: here; the scrollback is what a client gets before its first chunk.
        self.listeners: set[Callable[[str], None]] = set()
        self._pump: asyncio.Task | None = None

    # -- output ---------------------------------------------------------

    def _emit(self, chunk: str) -> None:
        self.scrollback = (self.scrollback + chunk)[-SCROLLBACK_CHARS:]
        for fn in list(self.listeners):
            try:
                fn(chunk)
            except Exception:
                # A wedged client must never stall the pump or the child.
                self.listeners.discard(fn)

    async def _read_forever(self) -> None:
        """Pump the child's merged stdout/stderr into listeners.

        Reads chunks rather than lines: a REPL prompt has no newline, so
        ``readline`` would sit on it until the user typed something, which
        is precisely the moment the prompt needed to be visible.
        """
        assert self.proc.stdout is not None
        try:
            while True:
                data = await self.proc.stdout.read(4096)
                if not data:
                    break
                self._emit(data.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(f"\n[genesis] stopped reading output: {exc}\n")
        finally:
            code = self.proc.returncode
            if code is not None:
                self._emit(f"\n[genesis] brain.py exited with code {code}\n")

    def start_pump(self) -> None:
        self._pump = asyncio.ensure_future(self._read_forever())

    async def drain(self, timeout: float = 1.0) -> None:
        """Wait for the pump to reach EOF, so ``scrollback`` is complete.

        A dead child's output is still in flight when ``wait()`` returns;
        reporting the failure before this has run yields an empty traceback.
        Shielded, because the caller may be on a timeout of its own and
        cancelling the pump would truncate exactly what it came for.
        """
        if self._pump is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self._pump), timeout)

    # -- input ----------------------------------------------------------

    def write(self, text: str) -> None:
        """Send a line to the child's stdin (what the terminal panel types)."""
        if self.proc.stdin is None or self.proc.returncode is not None:
            return
        # No flush: this is an asyncio StreamWriter, so the bytes are handed
        # to the transport here and go out on the next loop pass. There is no
        # flush() on it, and drain() would need this method to be async for
        # backpressure that a terminal line never generates.
        with contextlib.suppress(Exception):
            self.proc.stdin.write(text.encode("utf-8"))

    def close_stdin(self) -> None:
        """EOF on stdin - how a REPL is asked to end without a signal."""
        if self.proc.stdin is not None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()

    # -- lifecycle ------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.alive,
            "path": str(self.project),
            "pid": self.proc.pid,
            "exit_code": self.proc.returncode,
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "synapse_url": self.synapse_url,
        }

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
        if not self.alive:
            return
        self.close_stdin()
        with contextlib.suppress(ProcessLookupError):
            self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=_TERM_GRACE_S)
        except (asyncio.TimeoutError, Exception):
            with contextlib.suppress(ProcessLookupError, Exception):
                self.proc.kill()
            with contextlib.suppress(Exception):
                await self.proc.wait()


#: Running brains, keyed by resolved project path.
_BRAINS: dict[str, Brain] = {}


def _key(project: Path) -> str:
    return str(project)


def get(raw_path: str) -> Brain | None:
    """The brain for a project, if one is running. Reaps a dead one."""
    project = Path(raw_path).expanduser().resolve()
    brain = _BRAINS.get(_key(project))
    if brain is not None and not brain.alive:
        # Keep it around long enough to report the exit, but stop calling it
        # running - the UI distinguishes "never started" from "died".
        return brain
    return brain


def status(raw_path: str) -> dict[str, Any]:
    brain = get(raw_path)
    if brain is None:
        return {"running": False, "path": raw_path, "pid": None,
                "exit_code": None, "started_at": None, "uptime_s": None,
                "synapse_url": ""}
    return brain.status()


def _managed_synapse_url(project: Path) -> str:
    """The Genesis-started synapse serving this project's namespace, if any.

    Keyed on the namespace in the project's ``config.py``, because that is
    what the brain will register under: a synapse on another namespace is a
    different bus as far as this project is concerned, however green its pill.
    """
    namespace = _gs.read_namespace(project)
    if not namespace:
        return ""
    return _gs.managed_urls().get(namespace, "")


async def start(raw_path: str, *, synapse_url: str | None = None) -> dict[str, Any]:
    """Spawn ``python -u brain.py`` for a project. Idempotent.

    Raises :class:`BrainExited` when the child is gone within
    ``_FAST_EXIT_GRACE_S`` - see the module docstring.
    """
    project = Path(raw_path).expanduser().resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"{project} is not a directory")
    entry = project / "brain.py"
    if not entry.is_file():
        raise FileNotFoundError(
            "This project has no brain.py, so there is nothing to run. "
            "brain.py is the entry point that attaches the receptors.",
        )

    existing = _BRAINS.get(_key(project))
    if existing is not None and existing.alive:
        return existing.status()

    env = dict(os.environ)
    # Belt and braces with -u: some layers honour one and not the other.
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")

    url = (synapse_url or "").strip() or _managed_synapse_url(project)
    if url:
        env["SYNAPSE_URL"] = url
    else:
        # An inherited SYNAPSE_URL from whatever shell launched Genesis would
        # silently override config.py's in-process default for every project.
        env.pop("SYNAPSE_URL", None)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "brain.py",
        cwd=str(project),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # Merged, because the two interleave in a terminal and a tester
        # wants the traceback next to the prompt that produced it.
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    brain = Brain(project, proc, synapse_url=url)
    brain.start_pump()

    # Did it survive its own imports? See "Reporting a brain that died on the
    # way up" in the module docstring.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(proc.wait()),
                               timeout=_FAST_EXIT_GRACE_S)
    if proc.returncode is not None:
        await brain.drain()
        raise BrainExited(proc.returncode, brain.scrollback)

    _BRAINS[_key(project)] = brain
    return brain.status()


async def stop(raw_path: str) -> dict[str, Any]:
    project = Path(raw_path).expanduser().resolve()
    brain = _BRAINS.pop(_key(project), None)
    if brain is None:
        return {"running": False, "path": str(project), "pid": None,
                "exit_code": None, "started_at": None, "uptime_s": None,
                "synapse_url": ""}
    await brain.stop()
    out = brain.status()
    out["stopped"] = True
    return out


async def shutdown() -> None:
    """Stop every brain this Genesis started. Called on server shutdown."""
    for brain in list(_BRAINS.values()):
        with contextlib.suppress(Exception):
            await brain.stop()
    _BRAINS.clear()
