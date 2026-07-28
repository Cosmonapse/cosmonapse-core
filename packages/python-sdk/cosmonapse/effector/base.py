"""
cosmonapse.effector.base
~~~~~~~~~~~~~~~~~~~~~~~~
Effector is the action wrapper for Cosmonapse - the synapse-side
participant that services TOOL_CALL signals, the way an Engram services
RECALL / IMPRINT. In nervous-system terms: Neurons think, Engrams
remember, Effectors act.

Effectors are addressed by ``effector_id`` (explicit) or
``effector_kind`` (typed). One Effector per tool family is the intended
deployment: filesystem, shell, websearch, fetch. Tool calls are part of
the TASK trace - they inherit the containing TASK's trace_id; the
parent_id chain proves causation.

This module defines:

  Effector             ABC every tool backend implements
  Effector.serve       protocol-hook Effector: @fx.on_tool_call - the
                       return value is emitted as the TOOL_RESULT -
                       plus the standard LifecycleHooks trio
                       (@on_connect / @on_refresh / @on_schedule)
  EffectorBinding      declarative binding the Axon stores at construction
  ToolOutcome          what invoke() returns
  EffectorTimeout      raised when a tool-call deadline elapses unanswered
  EffectorCancelled    raised when the containing TASK terminates mid-call
  EffectorNotBound     raised when a Neuron asks for an unwired binding
  EffectorOverloaded   raised by a backend that must shed load

Effectors are *not* Neurons. They do not think and never produce
AGENT_OUTPUT; a failed invocation surfaces as ``error`` on TOOL_RESULT
rather than an ERROR signal, so the parent TASK is not terminated. They
are mounted on a hosting Dendrite via ``dendrite.attach_effector(effector)``.

Signal pair: TOOL_CALL (request) / TOOL_RESULT (reply, correlated by
``parent_id == the TOOL_CALL's id``) - the same per-operation correlation
RECALL/RECALLED uses, so a future caller-side client can be built exactly
like EngramClient.

Host-side behaviour (the standard wiring pattern - mirrors ``Axon.host``
and ``Engram.host``):
  @effector.host.on_<signal>   deferred Dendrite decorator - queued at
                                module level, registered on the HOSTING
                                Dendrite once it connects this Effector
                                (i.e. during ``Dendrite.start()``),
                                subscription ensured. e.g.
                                @FX.host.on_final for trace-scoped cleanup
                                (TOOL_CALL/TOOL_RESULT servicing itself
                                stays ``@fx.on_tool_call`` - this is for
                                observing the *rest* of the protocol).
"""

from __future__ import annotations

import inspect
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cosmonapse._hooks import LifecycleHooks
from cosmonapse.envelope import SignalType

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite

logger = logging.getLogger(__name__)


class _EffectorHostProxy:
    """Deferred Dendrite signal decorators, declared on the Effector.

    ``@effector.host.on_<signal>(**filters)`` queues a handler registration
    at module level; the hosting Dendrite replays it right after it
    connects this Effector - during ``Dendrite.start()``, just after the
    Effector's own ``connect()`` and TOOL_CALL subscription - and ensures
    the matching inbound subscription. Mirrors ``Axon.host`` /
    ``Engram.host`` exactly, for the action side::

        FX = Effector.serve(effector_id="fs-effector", effector_kind="filesystem")

        @FX.on_tool_call
        async def handle(tool, args): ...

        @FX.host.on_final
        async def cleanup(sig): ...

        dendrite.attach_effector(FX)
        await dendrite.start()   # cleanup() is now live on `dendrite`

    Any ``Dendrite.on_*`` signal decorator with the standard
    ``(fn, *, neuron=, capability=, trace_id=)`` shape is accepted; the
    name is validated eagerly so a typo fails at import time, not at
    connect time.
    """

    #: Dendrite ``on_*`` methods with a non-standard registration shape.
    _UNSUPPORTED: frozenset[str] = frozenset({"on_discover", "on_trace"})

    def __init__(self, effector: Effector) -> None:
        self._effector = effector

    @staticmethod
    def _signal_type_for(name: str) -> SignalType | None:
        key = name[3:].removesuffix("_signal").upper()
        try:
            return SignalType[key]
        except KeyError:
            return None

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("on_") or name in self._UNSUPPORTED:
            raise AttributeError(
                f"effector.host has no decorator {name!r} - use the "
                f"Dendrite's on_<signal> family (e.g. on_final, "
                f"on_tool_result)"
            )
        st = self._signal_type_for(name)
        from cosmonapse.dendrite import Dendrite
        if st is None or not hasattr(Dendrite, name):
            raise AttributeError(
                f"effector.host.{name}: not a Dendrite signal decorator"
            )

        def register(fn: Any = None, **filters: Any) -> Any:
            def deco(f: Any) -> Any:
                if self._effector._host_regs is None:
                    self._effector._host_regs = []
                self._effector._host_regs.append((name, st, dict(filters), f))
                return f
            return deco(fn) if callable(fn) else deco
        return register


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectorBinding:
    """Declarative wiring of one Effector into an Axon.

    The Axon stores a list of these at construction time so the Neuron
    can address Effectors by a stable local name (e.g. ``"fs"``) rather
    than the deployment-specific effector_id. ``name`` is what the
    Neuron sees; ``directed_id`` and ``directed_type`` determine how
    TOOL_CALL is routed on the wire (they become ``directed.id`` /
    ``directed.type`` on the envelope).

    At least one of ``directed_id`` or ``directed_type`` must be set.
    ``directed_id`` (the effector_id) is preferred for predictable
    routing; ``directed_type`` (the effector_kind) is for slot-based
    routing where deployment owns the concrete impl.

    ``tools`` is the caller-side routing table: the tool names this
    binding serves. The Axon resolves a native tool call to a binding
    by (1) a binding whose ``tools`` lists the name, (2) a binding
    *named* after the tool, (3) the only binding, when there is
    exactly one. Leave it None on a single-binding Axon.
    """

    name: str
    directed_id: str | None = None
    directed_type: str | None = None
    default_deadline_ms: int | None = None
    tools: tuple[str, ...] | None = None

    def to_directed(self) -> Any:
        """Build a :class:`cosmonapse.envelope.Directed` addressing this Effector."""
        from cosmonapse.envelope import Directed
        return Directed(id=self.directed_id, type=self.directed_type)

    def __post_init__(self) -> None:
        if not self.directed_id and not self.directed_type:
            raise ValueError(
                f"EffectorBinding {self.name!r} requires directed_id= "
                f"(effector_id) or directed_type= (effector_kind), or both"
            )


@dataclass(frozen=True)
class ToolOutcome:
    """What an invoke() call returns to the caller.

    Exactly one of ``result`` / ``error`` should be set. ``error`` is a
    tool-level failure the calling Neuron is expected to react to (a
    missing file, a refused command) - it rides TOOL_RESULT and never
    terminates the parent TASK.
    """

    tool: str
    result: Any = None
    error: str | None = None
    call_id: str | None = None
    took_ms: int | None = None
    effector_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EffectorError(Exception):
    """Base for Effector-related exceptions."""


class EffectorTimeout(EffectorError):
    """Raised when a TOOL_CALL deadline elapses with no TOOL_RESULT."""


class EffectorCancelled(EffectorError):
    """Raised when the containing TASK terminates while a tool call is
    in flight (FINAL/ERROR on the trace, or Dendrite shutdown)."""


class EffectorNotBound(EffectorError):
    """Raised when a Neuron asks for an Effector binding name the Axon
    was not constructed with."""


class EffectorOverloaded(EffectorError):
    """Raised by an Effector backend when it must shed load. Surfaces as
    ``error`` on TOOL_RESULT rather than a separate ERROR signal so the
    parent TASK is not terminated."""


# ---------------------------------------------------------------------------
# Effector ABC
# ---------------------------------------------------------------------------


class Effector(ABC):
    """Action wrapper. One tool family per Effector instance.

    Every backend implements this exact interface. Subclasses set
    ``effector_id``, ``effector_kind`` and ``capabilities`` (the tool
    names served, e.g. ``["read", "write", "glob"]``) on construction.
    Lifecycle methods (``connect`` / ``close``) own backend resources
    (subprocesses, HTTP pools, spawned MCP servers). ``invoke`` is
    async; backends that wrap sync libraries dispatch to a threadpool.

    Effectors do not think. ``invoke`` performs exactly the named tool
    call and reports what happened; deciding *which* tool to call, and
    reacting to the outcome, is Neuron-side work.
    """

    effector_id: str
    effector_kind: str
    capabilities: list[str]
    version: str | None = None

    # Deferred host-side registrations (@effector.host.on_<signal>),
    # replayed onto the hosting Dendrite when it connects this Effector.
    # Class-level defaults - this ABC has no ``__init__`` (concrete
    # backends, including ``_ServedEffector``, set their own attributes on
    # construction); ``host`` lazily creates the instance list.
    _host_regs: list[tuple[str, Any, dict[str, Any], Any]] | None = None
    _host_regs_applied: bool = False

    # The hosting Dendrite, set by ``Dendrite.attach_effector`` / cleared by
    # ``detach_effector`` - the action-side analogue of ``Axon._dendrite`` /
    # ``Axon.dendrite`` and ``Engram._dendrite`` / ``Engram.dendrite``.
    _dendrite: Dendrite | None = None

    @property
    def dendrite(self) -> Dendrite | None:
        """The hosting Dendrite, once attached - see ``_dendrite`` above."""
        return self._dendrite

    @property
    def host(self) -> _EffectorHostProxy:
        """Deferred Dendrite decorators - see :class:`_EffectorHostProxy`."""
        return _EffectorHostProxy(self)

    async def _on_hosted(self, dendrite: Dendrite) -> None:
        """Called by the hosting Dendrite right after it connects this
        Effector (``start()``, after ``connect()``/TOOL_CALL subscription).
        Replays ``@effector.host.on_*`` registrations onto ``dendrite`` and
        ensures their subscriptions, exactly once per Effector instance -
        the Effector-side twin of ``Axon._on_register_emitted`` /
        ``Engram._on_hosted``."""
        if self._host_regs and not self._host_regs_applied:
            self._host_regs_applied = True
            for name, _st, filters, fn in self._host_regs:
                getattr(dendrite, name)(fn, **filters)
            await dendrite.ensure_subscribed(
                *{st for _, st, _, _ in self._host_regs})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Open backend resources (subprocess, HTTP pool, ...)."""

    @abstractmethod
    async def close(self) -> None:
        """Release backend resources."""

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    async def can_serve(self, tool: str) -> bool:
        """Whether this Effector serves ``tool``. Default: the tool name
        is in ``capabilities`` (an empty list means serve everything).
        Backends may override for dynamic tool sets."""
        return not self.capabilities or tool in self.capabilities

    @abstractmethod
    async def invoke(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        deadline_ms: int | None = None,
        trace_id: str | None = None,
    ) -> ToolOutcome:
        """Run one tool call and return the outcome.

        Tool-level failures (bad args, missing file, non-zero exit)
        must be reported as ``ToolOutcome(error=...)``, not raised - a
        Neuron is expected to read the error and react. Raise only for
        backend faults (broken subprocess, lost connection); the hosting
        Dendrite maps a raised exception onto TOOL_RESULT ``error``
        anyway, so the parent TASK is never terminated by a tool."""

    # ------------------------------------------------------------------
    # Protocol-hook Effectors
    # ------------------------------------------------------------------

    @classmethod
    def serve(
        cls,
        *,
        effector_id: str,
        effector_kind: str = "effector",
        version: str | None = None,
    ) -> _ServedEffector:
        """Build an Effector from the one protocol hook that matters.

        Cosmonapse does not build your tools - no registries, no
        frameworks. It gives you exactly the signal pair: a TOOL_CALL
        arrives, your handler runs, its return value is emitted as the
        TOOL_RESULT. What happens in between (dispatch tables, MCP
        sessions, subprocesses, sandboxing) is your code::

            FX = Effector.serve(effector_id="fs-effector",
                                effector_kind="filesystem")

            @FX.on_tool_call
            async def handle(tool, args):
                if tool == "read":
                    return {"content": open(args["path"]).read()}
                return None          # fall through / unknown

            dendrite.attach_effector(FX)

        The result is a plain Effector. Lifecycle follows the shared
        :class:`cosmonapse._hooks.LifecycleHooks` contract every other
        component uses: ``@FX.on_connect`` fires once when the hosting
        Dendrite connects the Effector at start(), ``@FX.on_schedule(
        every_s=N)`` loops run until stop(), ``@FX.on_refresh`` fires
        on ``await FX.refresh()``. Hooks receive the owner (the
        Effector) as first argument.
        """
        return _ServedEffector(
            effector_id=effector_id,
            effector_kind=effector_kind,
            version=version,
        )


#: kwargs an @on_tool_call handler may request by declaring the parameter.
_TOOL_HANDLER_KWARGS = frozenset({"call_id", "deadline_ms", "trace_id"})


class _ServedEffector(Effector, LifecycleHooks):
    """Concrete Effector with one tool surface - ``@on_tool_call``,
    whose return value is emitted as the TOOL_RESULT by the hosting
    Dendrite - plus the shared LifecycleHooks trio. Built by
    :meth:`Effector.serve`; not instantiated directly.
    """

    def __init__(
        self,
        *,
        effector_id: str,
        effector_kind: str,
        version: str | None,
    ) -> None:
        LifecycleHooks.__init__(self)
        self.effector_id = effector_id
        self.effector_kind = effector_kind
        self.capabilities = []
        self.version = version
        # TOOL_CALL handlers, tried in registration order.
        self._call_handlers: list[tuple[Callable[..., Any], frozenset[str]]] = []

    def on_tool_call(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register a TOOL_CALL handler; its RETURN VALUE IS EMITTED AS
        THE TOOL_RESULT - no manual publish. The handler receives
        ``(tool, args)``, plus ``call_id`` / ``deadline_ms`` /
        ``trace_id`` if declared as keyword parameters (a ``**kwargs``
        catch-all receives all three). Sync or async.

        Multiple handlers run in registration order; the first non-None
        return answers, None falls through (so a policy gate can sit in
        front of a proxy). A raise becomes ``error`` on the TOOL_RESULT
        - a tool never terminates the parent TASK. If every handler
        returns None the reply is an ``unhandled tool`` error."""
        wants: set[str] = set()
        try:
            sig = inspect.signature(fn)
            for pname, p in sig.parameters.items():
                if p.kind is inspect.Parameter.VAR_KEYWORD:
                    wants |= _TOOL_HANDLER_KWARGS
                    break
                if pname in _TOOL_HANDLER_KWARGS:
                    wants.add(pname)
        except (ValueError, TypeError):
            pass  # no inspectable signature: positional-only call
        self._call_handlers.append((fn, frozenset(wants)))
        return fn

    # -- Effector interface ----------------------------------------------

    async def can_serve(self, tool: str) -> bool:
        """A served Effector answers for every tool name once a handler
        is registered - routing between tools is the handler's job."""
        return bool(self._call_handlers)

    async def connect(self) -> None:
        """Called by the hosting Dendrite at start(): starts the
        ``@on_schedule`` loops and fires the ``@on_connect`` hooks."""
        self._launch_schedule()
        await self._fire_connect()

    async def close(self) -> None:
        """Called by the hosting Dendrite at stop()/detach: cancels
        the ``@on_schedule`` loops."""
        await self._stop_hooks()

    async def invoke(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        deadline_ms: int | None = None,
        trace_id: str | None = None,
    ) -> ToolOutcome:
        t0 = time.monotonic()
        for fn, wants in self._call_handlers:
            kwargs: dict[str, Any] = {}
            if "call_id" in wants:
                kwargs["call_id"] = call_id
            if "deadline_ms" in wants:
                kwargs["deadline_ms"] = deadline_ms
            if "trace_id" in wants:
                kwargs["trace_id"] = trace_id
            try:
                result = fn(tool, args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                return ToolOutcome(
                    tool=tool, call_id=call_id,
                    error=f"{type(exc).__name__}: {exc}",
                    took_ms=int((time.monotonic() - t0) * 1000),
                    effector_id=self.effector_id,
                )
            if result is None:
                continue
            if isinstance(result, ToolOutcome):
                return result
            return ToolOutcome(
                tool=tool, result=result, call_id=call_id,
                took_ms=int((time.monotonic() - t0) * 1000),
                effector_id=self.effector_id,
            )
        return ToolOutcome(
            tool=tool, call_id=call_id,
            error=f"unhandled tool {tool!r}: no on_tool_call handler answered",
        )
