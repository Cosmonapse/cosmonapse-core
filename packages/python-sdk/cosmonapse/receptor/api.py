"""
cosmonapse.receptor.api
~~~~~~~~~~~~~~~~~~~~~~~
The HTTP Receptor: one endpoint, all three dispatch shapes.

Every ``app.py`` in cosmonapse-examples hand-rolls the same FastAPI edge -
a lifespan that stands up the Dendrites, a POST body model, a
``dispatch_and_wait`` with a try/except for the timeout. ``ApiReceptor``
is that edge as a primitive::

    rx = ApiReceptor(dendrite=orch, neuron="agent", input_key="goal",
                     path="/run")
    app = rx.app(title="Cosmonapse Agent")

The endpoint is *one* route, and the caller picks the shape:

    POST /run  {"input": "...", "mode": "wait"}    -> JSON result
    POST /run  {"input": "...", "mode": "send"}    -> 202 + trace_id
    POST /run  {"input": "...", "mode": "stream"}  -> text/event-stream

``input`` may be a string (wrapped with ``input_key``) or an object (used
as the TASK input verbatim). ``GET /run/{trace_id}`` opens an SSE stream
onto a trace another caller started, via ``observe_pathway``.

Mount into an app you already have::

    existing_app.include_router(rx.router)

FastAPI is not a core dependency - ``pip install cosmonapse[receptor]``
(or just ``pip install fastapi uvicorn``) before using this module. The
import is deferred to first use so ``import cosmonapse`` stays light.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cosmonapse.receptor.base import (
    DispatchMode,
    Receptor,
    ReceptorError,
    ReceptorTimeout,
    ReceptorUnbound,
    signal_to_jsonable,
)

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite

_FASTAPI_HINT = (
    "ApiReceptor needs FastAPI: pip install 'cosmonapse[receptor]' "
    "(or pip install fastapi uvicorn)"
)


def _fastapi() -> Any:
    try:
        import fastapi
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise ModuleNotFoundError(_FASTAPI_HINT) from exc
    return fastapi


def sse(event: str, data: Any) -> str:
    """One server-sent-event frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


class ApiReceptor(Receptor):
    """HTTP interface onto the dispatch trio.

    ``neuron`` and ``capabilities`` are both optional; a body may also
    carry either, so one endpoint can front several Neurons.

    ``allowed_modes`` narrows what a caller may ask for - drop ``"send"``
    if every request must be answered, or pin it to ``{"stream"}`` for a
    push-only edge. ``default_mode`` is used when the body omits ``mode``.
    """

    def __init__(
        self,
        *,
        dendrite: Dendrite | None = None,
        neuron: str | None = None,
        capabilities: list[str] | None = None,
        path: str = "/dispatch",
        mode: DispatchMode = "wait",
        allowed_modes: set[str] | None = None,
        max_timeout_s: float = 600.0,
        host: str = "127.0.0.1",
        port: int = 8000,
        **kw: Any,
    ) -> None:
        kw.setdefault("receptor_id", "api-receptor")
        super().__init__(dendrite=dendrite, neuron=neuron,
                         capabilities=capabilities, **kw)
        self.default_mode = mode
        self.path = "/" + path.strip("/")
        self.allowed_modes = set(allowed_modes or {"send", "wait", "stream"})
        self.max_timeout_s = max_timeout_s
        # Where run() serves. Two HTTP Receptors sharing a (host, port) are
        # merged onto one app by the runner; give one a different port= to
        # split them into separate servers.
        self.host = host
        self.port = port
        self._extra_routes: list[tuple[str, str, Callable[..., Any], dict[str, Any]]] = []

    # ------------------------------------------------------------------
    # Extra routes (the GET /memory, GET /stats every example grows)
    # ------------------------------------------------------------------

    def route(self, path: str, *, methods: list[str] | None = None,
              **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Attach an ordinary FastAPI route to this Receptor's router::

            @rx.route("/memory")
            async def memory():
                return agent_memory.summary()
        """
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._extra_routes.append((path, "route", fn,
                                       {"methods": methods or ["GET"], **kw}))
            return fn
        return decorator

    # ------------------------------------------------------------------
    # Request handling - transport-free, so it is unit-testable
    # ------------------------------------------------------------------

    def parse(
        self, body: dict[str, Any] | None,
    ) -> tuple[Any, DispatchMode, float | None, dict[str, Any]]:
        """Body -> (raw input, mode, timeout, dispatch overrides)."""
        body = dict(body or {})
        raw = body.pop("input", None)
        if raw is None:
            # Tolerate a bare payload: {"goal": "..."} with no envelope.
            known = {"mode", "timeout_s", "neuron", "capabilities", "trace_id",
                     "context_ref", "meta"}
            leftovers = {k: v for k, v in body.items() if k not in known}
            raw = leftovers or None
            for k in leftovers:
                body.pop(k, None)
        mode = str(body.pop("mode", self.default_mode))
        if mode not in self.allowed_modes:
            raise ValueError(
                f"mode must be one of {sorted(self.allowed_modes)}, got {mode!r}"
            )
        timeout_s = body.pop("timeout_s", self._timeout_s)
        if timeout_s is not None:
            timeout_s = min(float(timeout_s), self.max_timeout_s)
        overrides = {k: v for k, v in body.items()
                     if k in {"neuron", "capabilities", "trace_id",
                              "context_ref", "meta"} and v is not None}
        return raw, mode, timeout_s, overrides  # type: ignore[return-value]

    async def handle(self, body: dict[str, Any] | None) -> Any:
        """The whole endpoint as one coroutine. Returns a JSON-able dict,
        or an SSE async-iterator when ``mode == "stream"``."""
        fastapi = _fastapi()
        try:
            raw, mode, timeout_s, overrides = self.parse(body)
        except ValueError as exc:
            raise fastapi.HTTPException(422, str(exc)) from exc

        if mode == "send":
            try:
                sig = await self.send(raw, **overrides)
            except ReceptorUnbound as exc:
                raise fastapi.HTTPException(422, str(exc)) from exc
            return {"accepted": True, "trace_id": sig.trace_id,
                    "signal_id": sig.id}

        if mode == "stream":
            return self.sse_stream(raw, timeout_s=timeout_s, **overrides)

        try:
            result = await self.ask(raw, timeout_s=timeout_s, **overrides)
        except ReceptorTimeout as exc:
            raise fastapi.HTTPException(504, str(exc)) from exc
        except ReceptorUnbound as exc:
            raise fastapi.HTTPException(422, str(exc)) from exc
        except ReceptorError as exc:
            raise fastapi.HTTPException(500, str(exc)) from exc
        return result

    async def sse_stream(
        self, raw: Any = None, *, timeout_s: float | None = None,
        **overrides: Any,
    ) -> AsyncIterator[str]:
        """Every Signal on one trace as SSE frames, then ``event: done``."""
        try:
            async for sig in self.iter_signals(raw, timeout_s=timeout_s,
                                               **overrides):
                yield sse(sig.type.value.lower(), signal_to_jsonable(sig))
        except ReceptorTimeout as exc:
            yield sse("error", {"message": str(exc), "timeout": True})
        except Exception as exc:
            yield sse("error", {"message": str(exc)})
        yield sse("done", {"ok": True})

    async def observe_stream(
        self, trace_id: str, *, timeout_s: float | None = None,
    ) -> AsyncIterator[str]:
        """SSE onto a trace someone else started - a second screen on a run."""
        pw = await self.dendrite.observe_pathway(trace_id)
        self._wire(pw)
        try:
            while True:
                try:
                    sig = await asyncio.wait_for(
                        pw.__anext__(), timeout_s or self._timeout_s,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield sse("error", {"message": "observe timed out",
                                        "timeout": True})
                    break
                yield sse(sig.type.value.lower(), signal_to_jsonable(sig))
        finally:
            await pw.close()
        yield sse("done", {"ok": True})

    # ------------------------------------------------------------------
    # FastAPI wiring
    # ------------------------------------------------------------------

    @property
    def router(self) -> Any:
        """An ``APIRouter`` carrying the dispatch endpoint and extra routes."""
        # _fastapi() first, purely for its error message: it turns a missing
        # soft dependency into _FASTAPI_HINT instead of a bare ImportError.
        # The names themselves are then imported statically, the way chat.py
        # does it, so `router` is a real APIRouter rather than Any. That
        # matters under `mypy --strict`: a decorator pulled off an Any is an
        # untyped decorator, which taints every function it wraps and needs
        # silencing - and the silencer that used to be here named an error
        # code mypy has never had ("untyped-decorator"; the real one is
        # "misc"), so it suppressed nothing and added an unused-ignore of its
        # own. Typing the router removes the error instead of muting it.
        _fastapi()
        from fastapi import APIRouter
        from fastapi.responses import StreamingResponse

        router = APIRouter()

        @router.post(self.path)
        async def dispatch(body: dict[str, Any] | None = None) -> Any:
            result = await self.handle(body)
            if hasattr(result, "__aiter__"):
                return StreamingResponse(
                    result, media_type="text/event-stream",
                    headers={"cache-control": "no-cache",
                             "x-accel-buffering": "no"},
                )
            return result

        @router.get(self.path + "/{trace_id}")
        async def observe(trace_id: str) -> Any:
            return StreamingResponse(
                self.observe_stream(trace_id),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache"},
            )

        for path, _kind, fn, kw in self._extra_routes:
            router.add_api_route(path, fn, **kw)
        return router

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    def http_mount(self) -> tuple[str, int, Any] | None:
        """This Receptor's router, and where it wants to be served."""
        return (self.host, self.port, self.router)

    async def run(self) -> int:
        """Serve this Receptor alone on its own host/port via uvicorn.

        Mounted on a Dendrite, prefer ``dendrite.run()`` - it merges every
        HTTP Receptor sharing a port into one app instead of racing two
        servers for the same socket.
        """
        from cosmonapse.receptor.runner import _serve_group

        return await _serve_group(self.host, self.port, [self])

    @asynccontextmanager
    async def lifespan(
        self, *dendrites: Any,
        setup: Callable[[], Any] | None = None,
        teardown: Callable[[], Any] | None = None,
    ) -> AsyncIterator[None]:
        """Open the stack for the app's lifetime.

        Two shapes, because an ASGI app is imported before there is an
        event loop to connect a synapse on:

            # you already have the Dendrites
            app = FastAPI(lifespan=lambda app: rx.lifespan(*dendrites))

            # or build them when the loop exists; setup() returns
            # (dendrites, orchestrator) and the orchestrator is bound here
            app = rx.app(setup=my_setup, teardown=my_teardown)
        """
        from contextlib import AsyncExitStack

        built: list[Any] = list(dendrites)
        if setup is not None:
            result = setup()
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, tuple) and len(result) == 2:
                made, orchestrator = result
                built.extend(made)
                self.bind(orchestrator)
            elif isinstance(result, (list, tuple)):
                built.extend(result)
            elif result is not None:
                built.append(result)
                self.bind(result)
        try:
            # The Dendrites stop when this stack unwinds, and stopping
            # publishes DEREGISTER - so teardown (which typically closes
            # the Synapse) has to run strictly AFTER it, not inside it.
            async with AsyncExitStack() as stack:
                for d in built:
                    await stack.enter_async_context(d)
                yield
        finally:
            if teardown is not None:
                result = teardown()
                if hasattr(result, "__await__"):
                    await result

    def app(
        self, *, title: str = "Cosmonapse Receptor",
        dendrites: list[Any] | None = None,
        setup: Callable[[], Any] | None = None,
        teardown: Callable[[], Any] | None = None,
        **kw: Any,
    ) -> Any:
        """A standalone FastAPI app carrying just this Receptor.

        ``dendrites=[...]`` opens Dendrites you already built; ``setup=``
        builds them inside the lifespan (and binds the orchestrator it
        returns), which is what a module-level ``uvicorn api:app`` needs.
        """
        fastapi = _fastapi()
        if dendrites or setup is not None:
            def _lifespan(app: Any) -> Any:
                return self.lifespan(*(dendrites or []), setup=setup,
                                     teardown=teardown)
            kw.setdefault("lifespan", _lifespan)
        application = fastapi.FastAPI(title=title, **kw)
        application.include_router(self.router)
        return application
