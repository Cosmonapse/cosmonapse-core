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
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

#: How much of the child's output to keep, so a client connecting late still
#: sees the banner and prompt it missed rather than an empty screen.
SCROLLBACK_CHARS = 60_000

#: Grace period between asking a brain to stop and killing it.
_TERM_GRACE_S = 3.0


class Brain:
    """One running ``brain.py``, plus everyone watching its output."""

    def __init__(self, project: Path, proc: asyncio.subprocess.Process) -> None:
        self.project = project
        self.proc = proc
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
            except Exception:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            self._emit(f"\n[genesis] stopped reading output: {exc}\n")
        finally:
            code = self.proc.returncode
            if code is not None:
                self._emit(f"\n[genesis] brain.py exited with code {code}\n")

    def start_pump(self) -> None:
        self._pump = asyncio.ensure_future(self._read_forever())

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
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
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
                "exit_code": None, "started_at": None, "uptime_s": None}
    return brain.status()


async def start(raw_path: str) -> dict[str, Any]:
    """Spawn ``python -u brain.py`` for a project. Idempotent."""
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
    brain = Brain(project, proc)
    brain.start_pump()
    _BRAINS[_key(project)] = brain
    return brain.status()


async def stop(raw_path: str) -> dict[str, Any]:
    project = Path(raw_path).expanduser().resolve()
    brain = _BRAINS.pop(_key(project), None)
    if brain is None:
        return {"running": False, "path": str(project), "pid": None,
                "exit_code": None, "started_at": None, "uptime_s": None}
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
