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

Three rules, and they are the whole module:

1. **HTTP Receptors sharing a (host, port) merge into one app.** An
   ApiReceptor on ``/run`` and a ChatReceptor on ``/chat`` both bound to
   :8000 become a single FastAPI app with both routers included - one
   server, one port. Give one of them a different ``port=`` to split them.
2. **First to finish cancels the rest.** A foreground ``CliReceptor``
   exiting on ``:quit`` brings the whole process down, which is what the
   user just asked for. A server crashing does the same rather than
   leaving a half-dead brain.
3. **No Receptors blocks forever.** That is a headless worker node - a
   legitimate, common deployment. Poke it from outside with
   ``cosmo dispatch``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cosmonapse.receptor.base import Receptor

logger = logging.getLogger(__name__)

_UVICORN_HINT = (
    "Serving an HTTP Receptor needs uvicorn: "
    "pip install 'cosmonapse[receptor]'"
)


async def run_receptors(*receptors: "Receptor") -> int:
    """Run every Receptor concurrently until one of them finishes.

    Returns the finishing Receptor's exit code (0 when it returned None),
    after cancelling the others. With no Receptors, blocks forever.
    """
    if not receptors:
        await idle()
        return 0

    solo: list[Receptor] = []
    groups: dict[tuple[str, int], list[Receptor]] = {}
    for rx in receptors:
        mount = rx.http_mount()
        if mount is None:
            solo.append(rx)
        else:
            groups.setdefault((mount[0], mount[1]), []).append(rx)

    coros = [rx.run() for rx in solo]
    coros += [_serve_group(host, port, members)
              for (host, port), members in groups.items()]

    if len(coros) == 1:
        return _exit_code(await coros[0])

    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # Surface a crash rather than reporting the exit code of whichever
    # coroutine happened to unwind first.
    first = next(iter(done))
    for t in done:
        if t.exception() is not None:
            raise t.exception()  # type: ignore[misc]
    return _exit_code(first.result())


async def idle() -> None:
    """Block until cancelled. A brain with no interface still runs."""
    await asyncio.Event().wait()


def _exit_code(value: Any) -> int:
    return int(value) if isinstance(value, int) else 0


async def _serve_group(host: str, port: int,
                       receptors: list["Receptor"]) -> int:
    """Serve one or more HTTP Receptors as a single app on one port."""
    try:
        import uvicorn  # noqa: PLC0415
        from fastapi import FastAPI  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise ModuleNotFoundError(_UVICORN_HINT) from exc

    titles = ", ".join(getattr(rx, "title", None) or rx.receptor_id
                       for rx in receptors)
    app = FastAPI(title=titles)
    for rx in receptors:
        mount = rx.http_mount()
        if mount is not None:
            app.include_router(mount[2])

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info("serving %s on http://%s:%d", titles, host, port)
    await server.serve()
    return 0
