"""
cosmonapse.pathway
~~~~~~~~~~~~~~~~~~
The Pathway primitive  -  a per-trace event handle.

A Pathway is the client-side observation surface for one logical workflow,
identified by its ``trace_id``. Open one by calling
``dendrite.dispatch(neuron=..., input=...)`` (you become the *originator*),
or ``dendrite.observe_pathway(trace_id)`` to subscribe to a trace another
peer initiated (*observer* role). Every Signal whose ``trace_id`` matches
the Pathway is delivered into it.

The same primitive supports three consumption shapes  -  the dev picks the
one that fits the workflow:

* ``await pathway.wait()``  -  block until the next *terminal* Signal
  (AGENT_OUTPUT, CLARIFICATION, ERROR, or FINAL). The classic
  request/reply shape.
* ``@pathway.on(SignalType.X)``  -  register a callback that fires for each
  matching Signal as it arrives. The reactive shape  -  useful for streams
  like THOUGHT_DELTA or for cognition signals (PLAN / TOOL_CALL / …).
* ``async for sig in pathway:``  -  iterate over every Signal on this trace
  until the Pathway closes. The streaming shape.

The three shapes compose: callbacks, iteration, and ``wait()`` all observe
every Signal independently  -  broadcasting, not draining a queue.

Lifecycle
---------
A Pathway auto-closes on the first FINAL or ERROR Signal it sees (the
truly terminal types). It can also be closed explicitly by
``await pathway.close()`` or by exiting an ``async with`` block. The
Dendrite closes any still-open Pathways on ``stop()``.

A closed Pathway raises ``PathwayClosedError`` from ``wait()``/``wait_for()``,
yields no more values from iteration, and silently ignores further
``_deliver()`` calls.

Opt-in
------
This whole surface is additive. The existing ``dispatch_task`` /
``on_agent_output`` API is untouched. Use Pathways when you want
correlation, sequential composition, or trace-scoped subscriptions  -  and
ignore them when a global handler is enough.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from cosmonapse.envelope import Signal, SignalType

logger = logging.getLogger(__name__)


# Signals that auto-close a Pathway when received. FINAL and ERROR are
# the two truly terminal types  -  AGENT_OUTPUT alone does NOT close the
# Pathway because a streaming workflow may produce several before
# finalising.
_TERMINAL_TYPES: frozenset[SignalType] = frozenset({
    SignalType.FINAL,
    SignalType.ERROR,
})

# Default set that satisfies a bare ``wait()``  -  the first Signal of any
# of these types resolves the wait. ERROR / FINAL are included so a
# ``wait()`` doesn't hang on a failed or finalised workflow; CLARIFICATION
# and PERMISSION are included because both *pause* the workflow awaiting a
# human/peer decision, so a waiting orchestrator must surface them rather
# than hang. A caller that may receive these must inspect ``.type`` instead
# of assuming the resolved Signal is a final answer.
_WAIT_TYPES: frozenset[SignalType] = frozenset({
    SignalType.AGENT_OUTPUT,
    SignalType.CLARIFICATION,
    SignalType.PERMISSION,
    SignalType.ERROR,
    SignalType.FINAL,
})

# Signals delivered when scope="terminal"  -  the decentralised pattern:
# intermediate orchestration is handled peer-to-peer; the Cortex's Pathway
# only wakes for things that demand its attention: terminal events (FINAL /
# ERROR) and the two that need a human/peer decision before the workflow can
# proceed (CLARIFICATION / PERMISSION).
_SCOPE_TERMINAL_TYPES: frozenset[SignalType] = frozenset({
    SignalType.FINAL,
    SignalType.ERROR,
    SignalType.CLARIFICATION,
    SignalType.PERMISSION,
})

# Signal types that flow through a Pathway. Excludes:
#   * Management (REGISTER / DEREGISTER / HEARTBEAT / DISCOVER)  -  own
#     trace_id space, not workflow-correlated.
#   * TASK  -  the originator already knows it dispatched, and the worker
#     side doesn't need its own outbound TASKs replayed. Avoiding TASK
#     here also eliminates a double-subscription that would otherwise
#     occur when a Dendrite both hosts Axons (subscribes to TASK via
#     _on_task) and dispatches Pathways (which subscribes to PATHWAY_TYPES
#     via _dispatch_inbound).
PATHWAY_TYPES: frozenset[SignalType] = frozenset(
    t for t in SignalType
    if t not in {
        SignalType.TASK,
        SignalType.REGISTER,
        SignalType.DEREGISTER,
        SignalType.HEARTBEAT,
        SignalType.DISCOVER,
    }
)


SignalHandler = Callable[[Signal], Awaitable[None] | None]
PathwayCloseHook = Callable[["Pathway"], Awaitable[None] | None]


class PathwayClosedError(RuntimeError):
    """Raised when ``wait()`` is called on (or interrupted by) a closed Pathway."""


class Pathway:
    """A per-trace event handle. See module docstring for the full surface."""

    def __init__(
        self,
        trace_id: str,
        *,
        parent_id: str | None = None,
        role: str = "originator",
        on_close: PathwayCloseHook | None = None,
        scope: str = "all",
    ) -> None:
        """
        Construct a Pathway. Usually created by ``Dendrite.dispatch()`` or
        ``Dendrite.observe_pathway()``; direct construction is supported
        for tests and advanced uses.

        Parameters
        ----------
        trace_id
            The ``trace_id`` whose Signals this Pathway observes. Used for
            lifecycle grouping (a trace's terminal event closes pathways on
            that trace) even when correlation is per-operation.
        parent_id
            Optional per-operation correlation key. When set, the owning
            Dendrite routes inbound Signals to this Pathway by
            ``signal.parent_id == parent_id`` (request/reply) instead of by
            ``trace_id``. This is what lets a request/reply client such as
            ``EngramClient`` be a thin wrapper over a Pathway: it opens one
            keyed on its RECALL/IMPRINT id and awaits the matching response.
            ``None`` (default) is an ordinary trace-correlated Pathway.
        role
            ``"originator"`` if this Pathway was opened by dispatching a
            TASK, ``"observer"`` if opened to watch a trace started by a
            peer. Purely informational  -  the protocol does not distinguish.
        on_close
            Optional callback invoked once when the Pathway closes. Used
            by the Dendrite to evict the Pathway from its registry.
        scope
            ``"all"`` (default, centralised pattern): every PATHWAY_TYPES
            Signal on the trace is delivered. ``"terminal"`` (decentralised
            pattern): only FINAL / ERROR / CLARIFICATION / PERMISSION are
            delivered; intermediate AGENT_OUTPUT / PLAN / TOOL_CALL etc. are dropped
            on the Pathway side  -  other Dendrites on the bus still see and
            act on them. Use ``"terminal"`` when the orchestrator only
            wants to wake for workflow conclusion.
        """
        if scope not in ("all", "terminal"):
            raise ValueError(
                f"scope must be 'all' or 'terminal', got {scope!r}"
            )
        self._trace_id = trace_id
        self._parent_id = parent_id
        self._role = role
        self._on_close = on_close
        self._scope = scope
        self._scope_filter: frozenset[SignalType] | None = (
            _SCOPE_TERMINAL_TYPES if scope == "terminal" else None
        )

        self._iter_queue: asyncio.Queue[Signal | None] = asyncio.Queue()
        self._handlers: dict[SignalType, list[SignalHandler]] = {}
        self._waiters: list[
            tuple[frozenset[SignalType], asyncio.Future[Signal]]
        ] = []
        self._buffered_signals: list[Signal] = []
        self._closed = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def parent_id(self) -> str | None:
        """Per-operation correlation key, or ``None`` for a trace-correlated
        Pathway. See ``__init__``."""
        return self._parent_id

    @property
    def role(self) -> str:
        """``"originator"`` or ``"observer"``. Local label; protocol-invisible."""
        return self._role

    @property
    def scope(self) -> str:
        """``"all"`` or ``"terminal"``. Filters which Signal types are
        delivered (see __init__ docs). FINAL/ERROR always close the
        Pathway regardless of scope."""
        return self._scope

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # Consumer shape #1: wait
    # ------------------------------------------------------------------

    async def wait(self, timeout_s: float | None = None) -> Signal:
        """Resolve on the next AGENT_OUTPUT, CLARIFICATION, PERMISSION, ERROR, or FINAL.

        Raises ``asyncio.TimeoutError`` if ``timeout_s`` elapses first, and
        ``PathwayClosedError`` if the Pathway closes before any matching
        Signal arrives.
        """
        return await self._wait_for_types(_WAIT_TYPES, timeout_s=timeout_s)

    async def wait_for(
        self,
        signal_type: SignalType,
        timeout_s: float | None = None,
    ) -> Signal:
        """Resolve on the next Signal of the given type."""
        return await self._wait_for_types(
            frozenset({signal_type}), timeout_s=timeout_s,
        )

    async def _wait_for_types(
        self,
        types: frozenset[SignalType],
        timeout_s: float | None,
    ) -> Signal:
        # Serve from the buffer FIRST - even when the Pathway has since
        # closed. The terminal Signal can arrive (and auto-close the
        # Pathway) before the dispatcher's next wait() runs; already-
        # delivered Signals must remain consumable, e.g.
        # wait_for(AGENT_OUTPUT) then wait_for(FINAL) on a finalized trace.
        for i, sig in enumerate(self._buffered_signals):
            if sig.type in types:
                self._buffered_signals.pop(i)
                return sig
        if self._closed:
            raise PathwayClosedError(
                f"Pathway for trace {self._trace_id!r} is closed"
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Signal] = loop.create_future()
        entry = (types, fut)
        self._waiters.append(entry)
        try:
            if timeout_s is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout_s)
        finally:
            # Remove the entry if it's still queued (it was consumed if not).
            try:
                self._waiters.remove(entry)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Consumer shape #2: callbacks
    # ------------------------------------------------------------------

    def on(
        self, signal_type: SignalType,
    ) -> Callable[[SignalHandler], SignalHandler]:
        """Decorator: register a callback fired for each Signal of the given type.

        Usage::

            @pathway.on(SignalType.AGENT_OUTPUT)
            async def done(sig):
                ...
        """
        def decorator(fn: SignalHandler) -> SignalHandler:
            self._handlers.setdefault(signal_type, []).append(fn)
            return fn
        return decorator

    # ------------------------------------------------------------------
    # Consumer shape #3: async iteration
    # ------------------------------------------------------------------

    def __aiter__(self) -> "Pathway":
        return self

    async def __anext__(self) -> Signal:
        sig = await self._iter_queue.get()
        if sig is None:
            # close sentinel
            raise StopAsyncIteration
        return sig

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "Pathway":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the Pathway. Idempotent.

        Pending ``wait()`` calls are resolved with ``PathwayClosedError``;
        async iteration receives a stop sentinel; the ``on_close`` hook
        fires once.
        """
        if self._closed:
            return
        self._closed = True

        # 1. Fail any pending waiters so they don't hang forever.
        for _types, fut in self._waiters:
            if not fut.done():
                fut.set_exception(PathwayClosedError(
                    f"Pathway for trace {self._trace_id!r} closed "
                    f"before a matching Signal arrived"
                ))
        self._waiters.clear()

        # 2. Send close sentinel for any active async-for consumer.
        await self._iter_queue.put(None)

        # 3. Notify the owning Dendrite (or whoever opened this) so it can
        #    evict the Pathway from its registry.
        if self._on_close is not None:
            try:
                result = self._on_close(self)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001  -  teardown must not raise
                logger.warning(
                    "Pathway %s: on_close hook raised: %s",
                    self._trace_id, exc,
                )

    # ------------------------------------------------------------------
    # Internal: signal delivery
    # ------------------------------------------------------------------

    async def _deliver(self, signal: Signal) -> None:
        """Deliver a Signal to this Pathway.

        Called by the Dendrite when an inbound Signal's ``trace_id``
        matches this Pathway. Broadcasts to all three consumer shapes:

        1. resolves any pending ``wait()`` futures whose target type set
           includes this Signal's type;
        2. fires every callback registered for this Signal's type;
        3. pushes the Signal onto the async-iteration queue.

        With ``scope="terminal"``, Signal types outside FINAL / ERROR /
        CLARIFICATION are dropped here - they remain visible to other
        Dendrites on the bus. FINAL and ERROR always reach the auto-close
        path regardless of scope.
        """
        if self._closed:
            return

        # Scope filter - drop signals outside the configured set, but let
        # FINAL/ERROR through to auto-close so the Pathway can't be
        # stranded by a scope mismatch.
        if (
            self._scope_filter is not None
            and signal.type not in self._scope_filter
        ):
            # An explicitly registered callback is an explicit expression
            # of interest - fire it even though the scope filter drops the
            # Signal from wait()/iteration. This is what lets
            # dispatch_offer(scope="terminal") collect BIDs via its
            # @pathway.on(SignalType.BID) handler while the Pathway stays
            # quiet for everything else.
            for handler in self._handlers.get(signal.type, ()):
                try:
                    result = handler(signal)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Pathway %s: scoped-out handler for %s raised: %s",
                        self._trace_id, signal.type.value, exc,
                    )
            if signal.type in _TERMINAL_TYPES:
                await self.close()
            return

        # 1. Resolve matching waiters (broadcast: every waiter whose type
        #    set matches gets the Signal). Keep non-matching waiters.
        remaining: list[
            tuple[frozenset[SignalType], asyncio.Future[Signal]]
        ] = []
        consumed = False
        for types, fut in self._waiters:
            if signal.type in types and not fut.done():
                fut.set_result(signal)
                consumed = True
            else:
                remaining.append((types, fut))
        self._waiters = remaining
        if not consumed:
            self._buffered_signals.append(signal)

        # 2. Fire per-type callbacks. Exceptions are logged, not propagated
        #    - one buggy handler shouldn't break delivery to the others.
        for handler in self._handlers.get(signal.type, ()):
            try:
                result = handler(signal)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Pathway %s: handler for %s raised: %s",
                    self._trace_id, signal.type.value, exc,
                )

        # 3. Push to the iteration queue.
        await self._iter_queue.put(signal)

        # 4. Auto-close on terminal types.
        if signal.type in _TERMINAL_TYPES:
            await self.close()
