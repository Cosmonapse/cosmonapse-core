"""
cosmonapse.receptor.runner
~~~~~~~~~~~~~~~~~~~~~~~~~~
Run the Receptors mounted on a Dendrite.

This is the process-lifecycle half of the interface layer, kept out of
both ``Dendrite`` (which should not know what FastAPI is) and ``Receptor``
(which should not know what its siblings are doing). ``Dendrite.run()``
delegates here.

Note on the verb: ``Effector.serve()`` and ``Engram.serve()`` are
*constructors* - they build the protocol-hook flavour of a component and
attach to nothing. So "serve" is taken, and means something else. A
Receptor is *run*, the way ``CliReceptor.run()`` already reads.

Four rules, and they are the whole module:

1. **HTTP Receptors sharing a (host, port) merge into one app.** An
   ApiReceptor on ``/run`` and a ChatReceptor on ``/chat`` both bound to
   :8000 become a single FastAPI app with both routers included - one
   server, one port. Give one of them a different ``port=`` to split them.
2. **The brain is not bound to its interfaces.** A Receptor finishing
   detaches that interface and nothing else: its siblings keep serving and
   the brain keeps running. ``:quit`` in a REPL closes the REPL, it does not
   kill the process the Axons are hosted in - a Receptor is one of four
   attachments, not the thing the brain exists for. What ends a brain is a
   signal: Ctrl-C, or ``SIGTERM`` from Genesis's Stop button.
3. **A Receptor that raises still propagates.** A crash is a real failure
   and must not be swallowed by rule 2 - the brain comes down with a
   traceback rather than quietly serving one interface fewer.
4. **Nothing left to serve blocks forever.** Zero Receptors, or every
   Receptor finished, is a headless worker node - a legitimate, common
   deployment. Poke it from outside with ``cosmo dispatch``.

The one exception to rule 2, and it is not really one: a *one-shot* command
(``python brain.py greet --name Ada``) sets ``ends_process`` on its Receptor
before returning. That is the whole invocation completing, not an interface
dying, so the process exits with its code. A REPL and a server never set it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cosmonapse.receptor.base import Receptor

logger = logging.getLogger(__name__)

#: How long a cancelled HTTP group gets to unwind before we stop waiting.
_SHUTDOWN_GRACE_S = 5.0

_UVICORN_HINT = (
    "Serving an HTTP Receptor needs uvicorn: "
    "pip install 'cosmonapse[receptor]'"
)


async def run_brain(*dendrites: Any) -> int:
    """Run a brain: start every Dendrite, serve every interface, stop cleanly.

    This is the brain-level entry, and the one a ``brain.py`` should call.
    ``Dendrite.run()`` is *Dendrite*-scoped - it serves the Receptors mounted
    on one Dendrite - so using it as the process entry quietly makes one
    Dendrite the thing the process exists for. With a Dendrite per node that
    is not even expressible: there is no single one to call it on. A brain is
    its nodes, so what runs is the brain.

    Lifecycle is owned here on purpose. The alternative is an ``AsyncExitStack``
    in every entry file, which is ceremony that says nothing about the brain -
    and getting it wrong means a node that never registered, or a synapse held
    open by a Dendrite that was never stopped.

    Ordering matters and is deliberate: every Dendrite is started *before* any
    interface is served, so a Receptor cannot take a request for an Axon whose
    REGISTER has not gone out yet.

    Returns what ``run_receptors`` returns - see the rules in this module's
    docstring. A brain with no interfaces at all blocks as a headless node.
    """
    async with contextlib.AsyncExitStack() as stack:
        for dendrite in dendrites:
            await stack.enter_async_context(dendrite)
        receptors: list[Receptor] = []
        for dendrite in dendrites:
            receptors.extend(dendrite.receptors)
        return await run_receptors(*receptors)
    return 0        # pragma: no cover - AsyncExitStack never swallows


async def run_receptors(*receptors: Receptor) -> int:
    """Serve every Receptor concurrently for as long as the brain lives.

    Returns only when a Receptor signals that the *invocation* is over by
    setting ``ends_process`` (a one-shot command), in which case its exit
    code is returned. Otherwise this blocks: interfaces finishing is normal
    and does not end it, so the caller comes back via cancellation - which is
    what a signal handler does - or via a Receptor raising.
    """
    if not receptors:
        await idle()
        return 0

    _warn_on_contended_stdin(receptors)

    solo: list[Receptor] = []
    groups: dict[tuple[str, int], list[Receptor]] = {}
    for rx in receptors:
        mount = rx.http_mount()
        if mount is None:
            solo.append(rx)
        else:
            groups.setdefault((mount[0], mount[1]), []).append(rx)

    # Two different questions. Quiet is about *sharing* a terminal - a CLI
    # Receptor is printing here, so the access log stays out of its way
    # whether it REPLs or runs one command. The address line is about a
    # REPL specifically: worth saying when the process is about to sit
    # there, pure noise on `brain.py greet --name Ada`.
    quiet = _stdout_is_contended(solo)
    if quiet and any(rx.owns_terminal() for rx in solo):
        # Before anything is scheduled, so the address lands above the REPL's
        # banner rather than racing it into the middle of a prompt.
        for (host, port), members in groups.items():
            print(f"{_titles(members)} on http://{host}:{port}", flush=True)
    coros = [rx.run() for rx in solo]
    coros += [_serve_group(host, port, members, quiet=quiet)
              for (host, port), members in groups.items()]

    # Keep each task next to the Receptor that owns it, so a finishing task
    # can be asked whether its completion was an interface detaching or the
    # whole invocation ending (rule 2 and its exception).
    owners: list[Receptor | None] = list(solo) + [None] * len(groups)
    tasks = [asyncio.ensure_future(c) for c in coros]

    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                # Rule 3: a crash is not an interface finishing.
                if t.exception() is not None:
                    raise t.exception()  # type: ignore[misc]
                owner = owners[tasks.index(t)]
                if owner is not None and getattr(owner, "ends_process", False):
                    # The invocation is over, so its siblings are too. Stop
                    # them the same way a signal would rather than leaving
                    # them for the loop to cancel on the way out: an HTTP
                    # group torn down by interpreter shutdown never gets to
                    # unwind its lifespan, and logs a CancelledError
                    # traceback that makes a clean one-shot look like a
                    # crash.
                    await _stop(pending)
                    return _exit_code(t.result())
                logger.info(
                    "receptor %s finished; the brain keeps running",
                    getattr(owner, "receptor_id", "http"),
                )
    except asyncio.CancelledError:
        # A signal, almost always. Take the interfaces down with us.
        await _stop(tasks)
        raise
    else:
        # Rule 4: every interface has finished and none of them owned the
        # invocation. Same state as a brain with no Receptors at all.
        await idle()
        return 0


def _warn_on_contended_stdin(receptors: tuple[Receptor, ...]) -> None:
    """Two REPLs on one stdin is a wiring mistake, not a topology.

    A CliReceptor with no command on argv reads stdin with ``input()``. Mount
    two and they race for every line: both print a banner, and which one gets
    your text is down to which executor won. Cheap to do by accident - add a
    second CLI interface to a scaffolded project and there it is - and baffling
    to diagnose from the doubled prompt alone, so say it out loud. A warning
    rather than an error: they are legal, and one of them may be there to be
    driven programmatically.
    """
    repls = [rx for rx in receptors
             if rx.http_mount() is None and hasattr(rx, "repl")]
    if len(repls) > 1:
        logger.warning(
            "%d terminal Receptors are mounted (%s) and all of them read the "
            "same stdin - they will race for your input. Keep one, or give "
            "the others a command on argv.",
            len(repls), ", ".join(rx.receptor_id for rx in repls),
        )


async def _stop(tasks: Iterable[asyncio.Future]) -> None:
    """Cancel every still-running interface and wait for it to unwind.

    Waiting is the point: a Receptor's cancellation is where it gets to shut
    down properly - an HTTP group turns it into uvicorn's own graceful
    shutdown - and that only happens if someone awaits it.
    """
    live = [t for t in tasks if not t.done()]
    for t in live:
        t.cancel()
    if live:
        await asyncio.gather(*live, return_exceptions=True)


def _titles(receptors: list[Receptor]) -> str:
    """What to call an app that several Receptors share."""
    return ", ".join(getattr(rx, "title", None) or rx.receptor_id
                     for rx in receptors)


def _stdout_is_contended(receptors: list[Receptor]) -> bool:
    """Is a REPL drawing a prompt on the same stdout an HTTP server logs to?

    Two interfaces, one terminal: uvicorn's access log lands in the middle
    of the prompt the CliReceptor just drew, and the line you typed scrolls
    away under `INFO: 127.0.0.1:63512 - "POST /chat"`. Not a wiring mistake
    like two REPLs on one stdin - a chat window beside a terminal is a
    perfectly good brain - so the runner quiets the server rather than
    warning about it. Serve the HTTP Receptor on its own and it logs exactly
    as it always did.

    A one-shot command counts too: `brain.py greet --name Ada` should print
    its answer and nothing else, even though an HTTP sibling was mounted
    and served for the second it took.
    """
    return any(rx.http_mount() is None and hasattr(rx, "repl")
               for rx in receptors)


async def idle() -> None:
    """Block until cancelled. A brain with no interface still runs."""
    await asyncio.Event().wait()


def _exit_code(value: Any) -> int:
    return int(value) if isinstance(value, int) else 0


async def _serve_group(host: str, port: int,
                       receptors: list[Receptor],
                       *, quiet: bool = False) -> int:
    """Serve one or more HTTP Receptors as a single app on one port.

    ``quiet`` is set by the runner when a REPL owns this process's stdout
    (see ``_stdout_is_contended``): uvicorn's banner and access log are
    silenced. The one line worth keeping - where it is served - is printed
    by the runner instead, before any of this is scheduled.
    """
    try:
        import uvicorn
        from fastapi import FastAPI
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise ModuleNotFoundError(_UVICORN_HINT) from exc

    titles = _titles(receptors)
    app = FastAPI(title=titles)
    for rx in receptors:
        mount = rx.http_mount()
        if mount is not None:
            app.include_router(mount[2])

    config = uvicorn.Config(app, host=host, port=port,
                            log_level="warning" if quiet else "info",
                            access_log=not quiet)
    server = uvicorn.Server(config)
    logger.info("serving %s on http://%s:%d", titles, host, port)

    # Shielded so that cancellation - a signal, or a one-shot command
    # ending the invocation - reaches us first and is turned into uvicorn's
    # own graceful shutdown. Cancelled mid-``serve()``, uvicorn logs the
    # CancelledError its lifespan task dies on, and every one-shot command
    # ends on a traceback that reads like a crash but is just an interface
    # being asked to stop.
    task = asyncio.ensure_future(server.serve())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait({task}, timeout=_SHUTDOWN_GRACE_S)
        raise
    return 0
