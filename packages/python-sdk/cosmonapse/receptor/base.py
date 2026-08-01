"""
cosmonapse.receptor.base
~~~~~~~~~~~~~~~~~~~~~~~~
Receptor is the interface layer for Cosmonapse - the edge where a human
(or an outside system) touches the fabric. In nervous-system terms:
Neurons think, Engrams remember, Effectors act, Receptors *listen*.

A Receptor is the mirror image of an Effector. An Effector is inbound:
it services TOOL_CALL signals that the fabric emits. A Receptor is
outbound-originating: it takes something from the outside world - a
typed command, an HTTP request, a chat turn - turns it into a TASK,
and hands the trace back in whichever shape the transport wants.

The whole primitive is one funnel over the dispatch trio that already
exists on Dendrite::

    Receptor.send(input)     ->  dendrite.dispatch_task(...)        fire-and-forget
    Receptor.ask(input)      ->  dendrite.dispatch_and_wait(...)    request/reply
    Receptor.stream(input)   ->  dendrite.dispatch_and_subscribe(.) event stream

Every backend (CLI, API, chat) does exactly this and differs only in how
it collects ``input`` and renders the result. Nothing new crosses the
wire: a Receptor emits the same TASK an orchestrator Dendrite always
emitted, so `cosmo prism` sees no new signal types.

This module defines:

  Receptor             base class every interface backend extends
  DispatchMode         "send" | "wait" | "stream"
  ReceptorError        terminal ERROR signal surfaced to the caller
  ReceptorTimeout      no terminal Signal inside the deadline
  ReceptorUnbound      no Dendrite bound to dispatch from

Shaping hooks (all optional, all decorators)::

    @rx.on_input                 raw transport payload -> TASK input dict
    @rx.on_result                terminal Signal -> whatever the transport returns
    @rx.on_signal(SignalType.X)  intermediate trace signals (progress, tools, memory)
    @rx.on_failure               exception -> transport-shaped error value

Role
----
A Receptor requires an orchestrator-role Dendrite; the role guard lives
on ``dispatch`` itself, so a worker-role Dendrite raises at first use.
Receptors do not subscribe to anything on their own - the Pathway the
Dendrite already opens per trace is the only subscription involved.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from abc import ABC
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from cosmonapse.envelope import Signal, SignalType
from cosmonapse.pathway import Pathway

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite

logger = logging.getLogger(__name__)


#: The three consumption shapes a Receptor can hand back. Same TASK on the
#: wire in every case - only the caller's ergonomics change.
DispatchMode = Literal["send", "wait", "stream"]

#: Terminal Signal types ``Pathway.wait()`` resolves on. A Receptor treats
#: all of them as "the turn is over"; ``render`` decides what each means.
TERMINAL_TYPES: frozenset[SignalType] = frozenset({
    SignalType.AGENT_OUTPUT,
    SignalType.FINAL,
    SignalType.ERROR,
    SignalType.CLARIFICATION,
    SignalType.PERMISSION,
})


class ReceptorError(RuntimeError):
    """A trace ended on an ERROR Signal (or the transport refused the input).

    ``signal`` is the ERROR Signal when there was one, else None.
    """

    def __init__(self, message: str, *, signal: Signal | None = None) -> None:
        super().__init__(message)
        self.signal = signal


class ReceptorTimeout(TimeoutError):
    """No terminal Signal arrived on the trace inside ``timeout_s``."""


class ReceptorUnbound(ValueError):
    """The Receptor has no Dendrite to dispatch from.

    Not about *targets*: a Receptor with neither ``neuron=`` nor
    ``capabilities=`` makes an open call (see :class:`Receptor`). This is
    the harder failure - nothing to dispatch *from* at all, which an ASGI
    app hits when it serves a request before ``bind()`` ran.
    """


InputHook = Callable[[Any], Any]
ResultHook = Callable[[Signal], Any]
FailureHook = Callable[[BaseException], Any]
SignalHook = Callable[[Signal], Awaitable[None]]


async def _maybe_await(value: Any) -> Any:
    """Let every hook be written sync or async, indifferently."""
    if inspect.isawaitable(value):
        return await value
    return value


class Receptor(ABC):
    """Interface primitive: outside world -> TASK -> outside world.

    ``dendrite`` must be orchestrator-role; it may be omitted here and
    supplied later with ``rx.bind(...)``.

    ``neuron`` and ``capabilities`` are both optional and both accepted by
    every backend. What is required is that *one of them* resolves by the
    time a TASK goes out - set either at construction, or pass either on
    any call to override, exactly as on ``Dendrite.dispatch``::

        rx = CliReceptor(dendrite=orch, neuron="assistant")        # addressed
        rx = CliReceptor(dendrite=orch, capabilities=["chat"])     # routed
        rx = CliReceptor(dendrite=orch)                            # open call
        await rx.ask("hi", capabilities=["chat"])

    A call with neither is an *open call*: the TASK goes out unaddressed on
    the broadcast subject and is answered by any ``catch_all=True`` Axon in
    the namespace (plus unfiltered ``@on_task_signal`` observers). Nothing
    answers if nothing opted in, so an open call into a namespace of ordinary
    Axons resolves as a dispatch timeout rather than an error - name a
    ``neuron=`` or ``capabilities=`` when you meant to address someone.

    Example::

        rx = CliReceptor(dendrite=orch, neuron="agent", input_key="goal")
        result = await rx.ask("summarise the Collatz conjecture")

    ``input_key`` is what a bare string gets wrapped in - examples use
    "prompt", "goal", "question". Pass a dict to bypass the wrapping.
    """

    #: Default mode when a backend does not say otherwise.
    default_mode: DispatchMode = "wait"

    #: Does this interface finishing mean the whole *invocation* is over?
    #: False for a REPL or a server - the brain outlives them, and the
    #: runner keeps going (see cosmonapse.receptor.runner, rule 2). A
    #: one-shot CLI command sets it True on itself before returning,
    #: because there the interface *is* the invocation.
    ends_process: bool = False

    def owns_terminal(self) -> bool:
        """Will this interface hold the terminal for the process's lifetime?

        True only for an interactive REPL. The runner asks so it can quiet
        an HTTP sibling's access log, which would otherwise land in the
        middle of the prompt (see ``runner._stdout_is_contended``). A
        one-shot command is not terminal-owning: it prints once and the
        invocation ends, so there is nothing for a log line to interrupt.
        """
        return False

    def __init__(
        self,
        *,
        dendrite: Dendrite | None = None,
        neuron: str | None = None,
        capabilities: list[str] | None = None,
        receptor_id: str = "receptor",
        input_key: str = "prompt",
        timeout_s: float | None = 60.0,
        scope: str = "all",
        finalize: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._dendrite = dendrite
        self._neuron = neuron
        self._capabilities = list(capabilities) if capabilities else None
        self.receptor_id = receptor_id
        self._input_key = input_key
        self._timeout_s = timeout_s
        self._scope = scope
        self._finalize = finalize
        self._meta = dict(meta or {})

        self._input_hook: InputHook | None = None
        self._result_hook: ResultHook | None = None
        self._failure_hook: FailureHook | None = None
        self._signal_hooks: dict[SignalType, list[SignalHook]] = {}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def dendrite(self) -> Dendrite:
        """The orchestrator this Receptor dispatches from.

        Raises :class:`ReceptorUnbound` if none was given - an ASGI app
        built at import time binds its Dendrite in the lifespan instead
        (see :meth:`bind`).
        """
        if self._dendrite is None:
            raise ReceptorUnbound(
                f"{type(self).__name__} has no dendrite - pass "
                f"dendrite=... at construction or call rx.bind(dendrite) "
                f"once the synapse is up"
            )
        return self._dendrite

    @property
    def bound(self) -> bool:
        return self._dendrite is not None

    def bind(self, dendrite: Dendrite) -> Receptor:
        """Attach (or swap) the orchestrator Dendrite. Returns self.

        This is the late-binding hook for module-level ASGI apps: build the
        Receptor at import, connect the synapse in the lifespan, bind there.
        """
        self._dendrite = dendrite
        return self

    @property
    def target(self) -> dict[str, Any]:
        """The dispatch target as kwargs - useful for logging and tests."""
        return {"neuron": self._neuron, "capabilities": self._capabilities}

    # ------------------------------------------------------------------
    # Shaping hooks
    # ------------------------------------------------------------------

    def on_input(self, fn: InputHook) -> InputHook:
        """Transport payload -> TASK input dict. Sync or async.

        Replaces the default ``{input_key: text}`` wrapping entirely::

            @rx.on_input
            def build(raw):
                return {"goal": raw, "max_steps": 8}
        """
        self._input_hook = fn
        return fn

    def on_result(self, fn: ResultHook) -> ResultHook:
        """Terminal Signal -> what the transport hands back. Sync or async.

        Default: raise on ERROR, else ``payload["output"]`` (falling back
        to the whole payload)::

            @rx.on_result
            def render(sig):
                return sig.payload["output"]["report"]
        """
        self._result_hook = fn
        return fn

    def on_failure(self, fn: FailureHook) -> FailureHook:
        """Exception -> transport-shaped error value. Sync or async.

        Sees every failure a turn can produce - a terminal ERROR Signal
        (as :class:`ReceptorError`), a deadline (:class:`ReceptorTimeout`),
        and anything an ``on_input`` / ``on_result`` hook raised. Returning
        a value swallows the exception; re-raise inside the hook to
        propagate. :class:`ReceptorUnbound` is never routed here - an
        unbound Dendrite is a wiring mistake, not a runtime failure.
        """
        self._failure_hook = fn
        return fn

    def on_signal(
        self, *signal_types: SignalType,
    ) -> Callable[[SignalHook], SignalHook]:
        """Observe intermediate Signals on every trace this Receptor opens.

        This is the progress channel - PLAN, TOOL_CALL, RECALLED and the
        rest - and it is observation only; nothing here changes what
        crosses the wire::

            @rx.on_signal(SignalType.TOOL_CALL)
            async def show(sig):
                print("*", sig.payload["tool"])
        """
        def decorator(fn: SignalHook) -> SignalHook:
            for st in signal_types:
                self._signal_hooks.setdefault(st, []).append(fn)
            return fn
        return decorator

    # ------------------------------------------------------------------
    # Input / output shaping
    # ------------------------------------------------------------------

    async def build_input(self, raw: Any) -> dict[str, Any]:
        """Normalise whatever the transport collected into a TASK input."""
        if self._input_hook is not None:
            raw = await _maybe_await(self._input_hook(raw))
        if isinstance(raw, dict):
            return raw
        if raw is None:
            return {}
        return {self._input_key: raw}

    async def render(self, sig: Signal) -> Any:
        """Terminal Signal -> transport value. Raises on ERROR by default."""
        if self._result_hook is not None:
            return await _maybe_await(self._result_hook(sig))
        if sig.type is SignalType.ERROR:
            raise ReceptorError(
                str(sig.payload.get("message") or "task failed"), signal=sig,
            )
        payload = sig.payload or {}
        if "output" in payload:
            return payload["output"]
        return payload

    async def fail(self, exc: BaseException) -> Any:
        """Route an exception through ``on_failure``; re-raise if unhandled."""
        if self._failure_hook is None:
            raise exc
        return await _maybe_await(self._failure_hook(exc))

    # ------------------------------------------------------------------
    # The dispatch trio
    # ------------------------------------------------------------------

    async def send(self, raw: Any = None, **overrides: Any) -> Signal:
        """Fire-and-forget: emit the TASK, return the emitted Signal.

        No Pathway is opened, so ``on_signal`` hooks do not fire - use
        ``stream`` if you want to watch the trace.
        """
        kw = self._dispatch_kwargs(overrides)
        kw.pop("scope", None)
        kw.pop("timeout_s", None)
        return await self.dendrite.dispatch_task(
            input=await self.build_input(raw), **kw,
        )

    async def ask(self, raw: Any = None, **overrides: Any) -> Any:
        """Request/reply: dispatch, await the terminal Signal, render it.

        Equivalent to ``dispatch_and_wait`` plus the Receptor's rendering
        and progress hooks. Raises :class:`ReceptorTimeout` on deadline and
        :class:`ReceptorError` on a terminal ERROR (unless ``on_result`` or
        ``on_failure`` says otherwise).
        """
        timeout_s = overrides.pop("timeout_s", self._timeout_s)
        try:
            pw = await self.open(raw, **overrides)
            async with pw:
                sig = await pw.wait(timeout_s=timeout_s)
            return await self.render(sig)
        except asyncio.TimeoutError as exc:
            timeout = ReceptorTimeout(f"no terminal signal within {timeout_s}s")
            timeout.__cause__ = exc
            return await self.fail(timeout)
        except ReceptorUnbound:
            # An unbound Dendrite is a wiring mistake, not a runtime
            # failure - it must not be swallowed by an on_failure hook.
            raise
        except Exception as exc:
            # Everything else, ReceptorError from a terminal ERROR Signal
            # included, goes through fail(), which re-raises when no
            # on_failure hook is registered.
            return await self.fail(exc)

    async def stream(self, raw: Any = None, **overrides: Any) -> Pathway:
        """Event stream: dispatch and hand back the live Pathway.

        The caller iterates (``async for sig in pw``), attaches
        ``@pw.on(...)``, or holds it. Auto-closes on FINAL / ERROR.
        """
        return await self.open(raw, **overrides)

    async def open(self, raw: Any = None, **overrides: Any) -> Pathway:
        """Dispatch and return the wired Pathway. The shared root of the trio."""
        kw = self._dispatch_kwargs(overrides)
        kw.pop("timeout_s", None)
        pw = await self.dendrite.dispatch(
            input=await self.build_input(raw), **kw,
        )
        self._wire(pw)
        return pw

    async def receive(
        self, raw: Any = None, *, mode: DispatchMode | None = None,
        **overrides: Any,
    ) -> Any:
        """One funnel for all three shapes - what every backend calls.

        ``mode`` picks the trio member: "send" -> Signal, "wait" -> the
        rendered result, "stream" -> a live Pathway.
        """
        chosen = mode or self.default_mode
        if chosen == "send":
            return await self.send(raw, **overrides)
        if chosen == "stream":
            return await self.stream(raw, **overrides)
        if chosen == "wait":
            return await self.ask(raw, **overrides)
        raise ValueError(
            f"mode must be 'send', 'wait', or 'stream', got {chosen!r}"
        )

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> int:
        """Run this Receptor's transport until it finishes. Returns an exit code.

        ``run`` and not ``serve``: ``Effector.serve()`` / ``Engram.serve()``
        are constructors that build the protocol-hook flavour of a
        component, so the verb is taken and means something else. A
        Receptor is run, the way ``CliReceptor.run()`` reads.

        The base class has no transport - subclasses implement this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no transport to run. Use "
            f"CliReceptor, ApiReceptor or ChatReceptor, or call "
            f"ask/send/stream directly."
        )

    def http_mount(self) -> tuple[str, int, Any] | None:
        """``(host, port, router)`` for an HTTP-served Receptor, else None.

        Receptors that return the same ``(host, port)`` are merged into one
        app on one port by :func:`~cosmonapse.receptor.runner.run_receptors`.
        """
        return None

    # ------------------------------------------------------------------
    # Streaming helper shared by the API and chat backends
    # ------------------------------------------------------------------

    def terminal_types(self) -> frozenset[SignalType]:
        """Which Signal types end a turn for this Receptor.

        A plain trace ends on the worker's AGENT_OUTPUT. A ``finalize``
        trace promotes that to FINAL, so AGENT_OUTPUT is mid-flight there
        and the stream must keep reading.

        ``scope="terminal"`` says the same thing from the other side: the
        Pathway will not resolve on an AGENT_OUTPUT, so a stream reading
        the same trace must not stop on one either. That is the shape a
        choreographed brain has - some *other* node emits FINAL, so the
        Receptor cannot set ``finalize`` (that would promote the first
        worker's output) and yet AGENT_OUTPUT is still mid-flight.
        """
        if self._finalize or self._scope == "terminal":
            return frozenset(TERMINAL_TYPES - {SignalType.AGENT_OUTPUT})
        return TERMINAL_TYPES

    async def iter_signals(
        self, raw: Any = None, *, timeout_s: float | None = None,
        stop_on: frozenset[SignalType] | None = None, **overrides: Any,
    ):
        """Async-generate every Signal on one trace, terminal one included.

        The generator stops after yielding a terminal Signal - a stream has
        to end when the turn does, and only FINAL / ERROR auto-close the
        Pathway. Pass ``stop_on=frozenset()`` to read until the Pathway
        closes on its own. The Pathway is closed on the way out either way,
        so an abandoned HTTP stream cannot leak a subscription.
        """
        deadline = timeout_s if timeout_s is not None else self._timeout_s
        terminal = self.terminal_types() if stop_on is None else stop_on
        pw = await self.open(raw, **overrides)
        loop = asyncio.get_running_loop()
        end = None if deadline is None else loop.time() + deadline
        try:
            while True:
                try:
                    if end is None:
                        sig = await pw.__anext__()
                    else:
                        remaining = end - loop.time()
                        if remaining <= 0:
                            raise ReceptorTimeout(f"stream exceeded {deadline}s")
                        sig = await asyncio.wait_for(pw.__anext__(), remaining)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError as exc:
                    timeout = ReceptorTimeout(f"stream exceeded {deadline}s")
                    timeout.__cause__ = exc
                    raise timeout from exc
                yield sig
                if sig.type in terminal:
                    return
        finally:
            await pw.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _dispatch_kwargs(self, overrides: dict[str, Any]) -> dict[str, Any]:
        neuron = overrides.pop("neuron", self._neuron)
        capabilities = overrides.pop("capabilities", self._capabilities)
        # Neither is legal: an open call. See the class docstring for what
        # answers it, and why silence rather than an exception is the
        # honest outcome - the Receptor cannot know from here whether the
        # namespace has a catch_all Axon in it.
        meta = {**self._meta, **(overrides.pop("meta", None) or {})}
        meta.setdefault("receptor", self.receptor_id)
        kw: dict[str, Any] = {
            "neuron": neuron,
            "capabilities": capabilities,
            "scope": overrides.pop("scope", self._scope),
            "finalize": overrides.pop("finalize", self._finalize),
            "meta": meta,
        }
        for passthrough in ("trace_id", "parent_id", "context_ref"):
            if passthrough in overrides:
                kw[passthrough] = overrides.pop(passthrough)
        kw.update(overrides)
        return kw

    def _wire(self, pw: Pathway) -> None:
        """Attach the registered ``on_signal`` hooks to a fresh Pathway."""
        for st, hooks in self._signal_hooks.items():
            for hook in hooks:
                pw.on(st)(_guarded(hook))


def _guarded(hook: SignalHook) -> SignalHook:
    """A misbehaving progress hook must never break the trace it observes."""
    async def wrapper(sig: Signal) -> None:
        try:
            await _maybe_await(hook(sig))
        except Exception:
            logger.exception("receptor on_signal hook failed for %s", sig.type)
    return wrapper


def signal_to_jsonable(sig: Signal) -> dict[str, Any]:
    """Signal -> a small JSON dict, for SSE frames and chat transcripts."""
    return {
        "id": sig.id,
        "trace_id": sig.trace_id,
        "parent_id": sig.parent_id,
        "type": sig.type.value,
        "payload": _jsonable(sig.payload),
    }


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))
    return value
