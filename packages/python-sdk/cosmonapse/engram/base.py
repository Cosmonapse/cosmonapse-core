"""
cosmonapse.engram.base
~~~~~~~~~~~~~~~~~~~~~~
Engram is the storage wrapper for Cosmonapse. It is the synapse-side
participant that services RECALL / IMPRINT signals. See ENGRAM_DESIGN.md.

Engrams are addressed by ``engram_id`` (explicit) or ``engram_kind``
(typed). One Engram per purpose is the intended deployment: context,
vectors, blobs, relational records. Recall/imprint are part of the
TASK trace - they inherit the containing TASK's trace_id; the
parent_id chain proves causation.

This module defines:

  Engram               ABC every backend implements
  EngramBinding        declarative binding the Axon stores at construction
  Hit                  one search result from recall()
  RecallResult         what recall() returns
  ImprintReceipt       what imprint() returns
  EngramTimeout        raised when a recall deadline elapses with no answer
  EngramCancelled      raised when the containing TASK is terminated mid-call
  EngramNotBound       raised when a Neuron asks for an unwired binding

Engrams are *not* Neurons. They do not produce AGENT_OUTPUT. They are
mounted on a hosting Dendrite via ``dendrite.attach_engram(engram)``.

Two ways to write one. Subclass ``Engram`` and implement ``recall`` /
``imprint`` (what every backend in this package does), or build one from
decorators with ``Engram.serve()`` - the memory-side twin of
``Effector.serve()``::

    ENGRAM = Engram.serve(engram_id="notes", engram_kind="context")

    @ENGRAM.on_recall
    async def search(query, **kw): return [Hit(id=..., entry=...)]

    @ENGRAM.on_imprint
    async def write(op, entry, *, merge_key=None, **kw): return ...

The handler RUNS the operation and its return value becomes the RECALLED
hits / IMPRINTED receipt. Either way the Engram is attached under its own
``engram_id``, so it REGISTERs normally and observers classify it as an
Engram - the decorators change where the body lives, nothing else.

Note the difference from ``@engram.host.on_recall_signal`` below: the host
decorators OBSERVE the protocol (the Dendrite has already serviced the
request by the time they run), whereas ``@on_recall`` / ``@on_imprint``
ARE the servicing. Registering a host observer does not disable the
built-in path, so servicing from one would emit a second RECALLED /
IMPRINTED and, for non-idempotent ops, write twice.

Host-side behaviour (the standard wiring pattern - mirrors ``Axon.host``):
  @engram.host.on_<signal>   deferred Dendrite decorator - queued at
                              module level, registered on the HOSTING
                              Dendrite once it connects this Engram (i.e.
                              during ``Dendrite.start()`` / after
                              ``attach_engram``), subscription ensured.
                              e.g. @ENGRAM.host.on_imprint_signal - react
                              to writes without a hand-wired ``@host.on_*``
                              on the Dendrite instance itself.
"""

from __future__ import annotations

import inspect
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from cosmonapse._hooks import LifecycleHooks
from cosmonapse.envelope import SignalType

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite

logger = logging.getLogger(__name__)


class _EngramHostProxy:
    """Deferred Dendrite signal decorators, declared on the Engram.

    ``@engram.host.on_<signal>(**filters)`` queues a handler registration
    at module level; the hosting Dendrite replays it right after it
    connects this Engram - during ``Dendrite.start()`` (or a future live
    ``add_engram``), just after the Engram's own ``connect()`` and REGISTER
    - and ensures the matching inbound subscription. Mirrors
    ``Axon.host`` exactly, for the memory side::

        ENGRAM = InMemoryEngram(engram_id="session-memory", engram_kind="context")

        @ENGRAM.host.on_imprint_signal
        async def persist(sig): ...

        host = Dendrite(synapse=synapse, role="worker")
        host.attach_engram(ENGRAM)
        await host.start()   # persist() is now live on `host`

    Any ``Dendrite.on_*`` signal decorator with the standard
    ``(fn, *, neuron=, capability=, trace_id=)`` shape is accepted; the
    name is validated eagerly so a typo fails at import time, not at
    connect time.
    """

    #: Dendrite ``on_*`` methods with a non-standard registration shape.
    _UNSUPPORTED: frozenset[str] = frozenset({"on_discover", "on_trace"})

    def __init__(self, engram: "Engram") -> None:
        self._engram = engram

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
                f"engram.host has no decorator {name!r} - use the "
                f"Dendrite's on_<signal> family (e.g. on_imprint_signal, "
                f"on_recall_signal)"
            )
        st = self._signal_type_for(name)
        from cosmonapse.dendrite import Dendrite
        if st is None or not hasattr(Dendrite, name):
            raise AttributeError(
                f"engram.host.{name}: not a Dendrite signal decorator"
            )

        def register(fn: Any = None, **filters: Any) -> Any:
            def deco(f: Any) -> Any:
                if self._engram._host_regs is None:
                    self._engram._host_regs = []
                self._engram._host_regs.append((name, st, dict(filters), f))
                return f
            return deco(fn) if callable(fn) else deco
        return register


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngramBinding:
    """Declarative wiring of one Engram into an Axon.

    The Axon stores a list of these at construction time so the Neuron
    can address Engrams by a stable local name (e.g. ``"ctx"``) rather
    than the deployment-specific engram_id. ``name`` is what the Neuron
    sees; ``directed_id`` and ``directed_type`` determine how
    RECALL/IMPRINT are routed on the wire (they become ``directed.id`` /
    ``directed.type`` on the envelope).

    At least one of ``directed_id`` or ``directed_type`` must be set.
    ``directed_id`` (the engram_id) is preferred for predictable routing;
    ``directed_type`` (the engram_kind) is for slot-based routing where
    deployment owns the concrete impl.
    """

    name: str
    directed_id: str | None = None
    directed_type: str | None = None
    default_deadline_ms: int | None = None
    default_recall_mode: str = "first"

    def to_directed(self) -> "Any":
        """Build a :class:`cosmonapse.envelope.Directed` addressing this Engram."""
        from cosmonapse.envelope import Directed
        return Directed(id=self.directed_id, type=self.directed_type)

    def __post_init__(self) -> None:
        if not self.directed_id and not self.directed_type:
            raise ValueError(
                f"EngramBinding {self.name!r} requires directed_id= "
                f"(engram_id) or directed_type= (engram_kind), or both"
            )
        if self.default_recall_mode not in ("first", "merge", "all"):
            raise ValueError(
                f"EngramBinding {self.name!r}: default_recall_mode must be "
                f"'first' | 'merge' | 'all', got "
                f"{self.default_recall_mode!r}"
            )


@dataclass(frozen=True)
class Hit:
    """One search result. ``score`` is backend-dependent; semantic backends
    return cosine similarity in [0,1], relational backends typically 1.0."""

    id: str
    entry: dict[str, Any]
    score: float = 1.0


@dataclass(frozen=True)
class RecallResult:
    """What a recall() call returns to the caller.

    ``hits`` is the merged-and-sorted list across all responding Engrams
    when ``recall_mode`` is ``"merge"``, or just the first responder's
    hits when ``"first"``. For ``"all"`` callers should iterate the
    stream rather than awaiting this object.
    """

    hits: list[Hit] = field(default_factory=list)
    engram_ids: tuple[str, ...] = ()
    truncated: bool = False
    took_ms: int | None = None

    def __iter__(self) -> Iterator[Hit]:
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __bool__(self) -> bool:
        return bool(self.hits)


@dataclass(frozen=True)
class ImprintReceipt:
    """What an imprint() call returns to the caller."""

    engram_id: str
    op: str
    id: str | None = None
    version: int | None = None
    took_ms: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EngramError(Exception):
    """Base for Engram-related exceptions."""


class EngramTimeout(EngramError):
    """Raised when a RECALL or IMPRINT deadline elapses with no response."""


class EngramCancelled(EngramError):
    """Raised when the containing TASK terminates while a recall/imprint
    is in flight (FINAL/ERROR on the trace, or Dendrite shutdown)."""


class EngramNotBound(EngramError):
    """Raised when a Neuron asks for an Engram binding name the Axon
    was not constructed with."""


class EngramOverloaded(EngramError):
    """Raised by an Engram backend when it must shed load. Surfaces as
    an ``error`` on IMPRINTED rather than a separate ERROR signal so
    the parent TASK is not terminated."""


# ---------------------------------------------------------------------------
# Engram ABC
# ---------------------------------------------------------------------------


class Engram(ABC):
    """Storage wrapper. One backend per Engram instance.

    Every backend implements this exact interface. The conformance
    suite in ``tests/test_engram.py`` runs against any Engram and is
    the single source of truth for correct behaviour.

    Subclasses set ``engram_id``, ``engram_kind`` and ``capabilities``
    on construction. Lifecycle methods (``connect`` / ``close``) own
    backend resources (DB pools, file handles). All write/read methods
    are async; backends that wrap sync libraries (sqlite3) dispatch
    to a threadpool.
    """

    engram_id: str
    engram_kind: str
    capabilities: list[str]
    version: str | None = None

    # Deferred host-side registrations (@engram.host.on_<signal>), replayed
    # onto the hosting Dendrite when it connects this Engram. Class-level
    # ``None``/``False`` defaults (no ``__init__`` on this ABC - concrete
    # backends set their own attributes on construction, same reasoning as
    # ``_saga_journal`` below); ``host`` lazily creates the instance list.
    _host_regs: "list[tuple[str, Any, dict[str, Any], Any]] | None" = None
    _host_regs_applied: bool = False

    # The hosting Dendrite, set by ``Dendrite.attach_engram`` / cleared by
    # ``detach_engram`` - the memory-side analogue of ``Axon._dendrite`` /
    # ``Axon.dendrite``. Lets an ``@engram.host.on_*`` handler reach the
    # Dendrite it was replayed onto (e.g. to ``dispatch_task`` / call a
    # tool) without a hand-wired module-level reference.
    _dendrite: "Dendrite | None" = None

    @property
    def dendrite(self) -> "Dendrite | None":
        """The hosting Dendrite, once attached - see ``_dendrite`` above."""
        return self._dendrite

    @property
    def host(self) -> "_EngramHostProxy":
        """Deferred Dendrite decorators - see :class:`_EngramHostProxy`."""
        return _EngramHostProxy(self)

    async def _on_hosted(self, dendrite: "Dendrite") -> None:
        """Called by the hosting Dendrite right after it connects this
        Engram (``start()``, after ``connect()``/REGISTER). Replays
        ``@engram.host.on_*`` registrations onto ``dendrite`` and ensures
        their subscriptions, exactly once per Engram instance - the
        Engram-side twin of ``Axon._on_register_emitted``."""
        if self._host_regs and not self._host_regs_applied:
            self._host_regs_applied = True
            for name, st, filters, fn in self._host_regs:
                getattr(dendrite, name)(fn, **filters)
            await dendrite.ensure_subscribed(
                *{st for _, st, _, _ in self._host_regs})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Open backend resources (DB pool, file handle, ...)."""

    @abstractmethod
    async def close(self) -> None:
        """Release backend resources."""

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    @abstractmethod
    async def recall(
        self,
        query: dict[str, Any],
        *,
        filters: dict[str, Any] | None = None,
        context_ref: str | None = None,
        deadline_ms: int | None = None,
        min_confidence: float | None = None,
    ) -> list[Hit]:
        """Return matching entries. Empty list if nothing matches; do
        not raise on a miss. Honour ``deadline_ms`` cooperatively when
        the backend supports it."""

    @abstractmethod
    async def imprint(
        self,
        op: str,
        entry: dict[str, Any],
        *,
        merge_key: str | None = None,
        imprint_id: str | None = None,
        trace_id: str | None = None,
    ) -> ImprintReceipt:
        """Write to the backend. ``op`` is one of
        ``add | append | merge | upsert | delete``. ``imprint_id`` is
        the originating IMPRINT signal's id - use it for idempotency
        (return a no-op receipt on re-delivery).

        ``trace_id`` is the containing TASK's trace. When set, a backend
        that supports rollback records the *inverse* of this write in a
        per-trace saga journal (via :meth:`_saga_record`) so the whole
        trace can be reversed by :meth:`compensate`. A ``None`` trace_id
        means "do not journal" - which is exactly how :meth:`compensate`
        replays inverse ops without re-journaling them."""

    # ------------------------------------------------------------------
    # Saga / compensating-log rollback
    # ------------------------------------------------------------------
    # A backend opts in to rollback by calling ``_saga_record`` from inside
    # ``imprint`` with the *inverse* op needed to undo the write it is about
    # to apply (it already reads the prior row to do upsert/merge/delete, so
    # capturing the inverse is cheap). ``compensate`` then replays those
    # inverse ops in reverse order through the public ``imprint`` path with
    # ``trace_id=None`` (so they neither re-journal nor consume idempotency
    # keys). Because every inverse is itself a valid add/upsert/delete, this
    # is fully backend-agnostic - a backend needs no bespoke "undo" code.

    # Per-trace inverse-op journal. ``None`` (the immutable class default) until
    # the first journaled write; each instance then binds its own dict.
    _saga_journal: "dict[str, list[dict[str, Any]]] | None" = None

    def _saga_record(
        self,
        trace_id: str | None,
        op: str,
        entry: dict[str, Any],
        *,
        merge_key: str | None = None,
    ) -> None:
        """Append one inverse op to the trace's journal. No-op when
        ``trace_id`` is falsy (uncorrelated write, or a compensation replay)."""
        if not trace_id:
            return
        if self._saga_journal is None:
            self._saga_journal = {}
        self._saga_journal.setdefault(trace_id, []).append(
            {"op": op, "entry": entry, "merge_key": merge_key}
        )

    async def compensate(self, trace_id: str) -> int:
        """Reverse every journaled write for ``trace_id`` and discard the
        journal. Returns the number of inverse ops applied.

        Replays in reverse (LIFO) so nested overwrites unwind to the
        original state. Best-effort: a failing inverse is logged and the
        rest still run. Only Engram state is reversed - external side
        effects are out of scope (see :func:`cosmonapse.envelope.stop_signal`)."""
        if not self._saga_journal:
            return 0
        inverses = self._saga_journal.pop(trace_id, [])
        applied = 0
        for inv in reversed(inverses):
            try:
                await self.imprint(
                    inv["op"], inv["entry"],
                    merge_key=inv.get("merge_key"), trace_id=None,
                )
                applied += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Engram %s: compensation step %r failed: %s",
                    getattr(self, "engram_id", "?"), inv.get("op"), exc,
                )
        return applied

    async def commit(self, trace_id: str) -> None:
        """Discard the trace's saga journal without reversing anything.

        Called at the workflow commit point (FINAL/ERROR on the trace) so
        successful writes become permanent and the journal can't leak."""
        if self._saga_journal:
            self._saga_journal.pop(trace_id, None)

    # ------------------------------------------------------------------
    # Optional capability negotiation
    # ------------------------------------------------------------------

    async def can_serve(self, query: dict[str, Any]) -> bool:
        """Return False if this Engram cannot satisfy the query (e.g. a
        BM25 engram asked for vector search). The hosting Dendrite skips
        responding when this returns False. Default: serve everything."""
        return True

    # ------------------------------------------------------------------
    # Decorator-native construction
    # ------------------------------------------------------------------

    @classmethod
    def serve(
        cls,
        *,
        engram_id: str,
        engram_kind: str = "context",
        capabilities: list[str] | None = None,
        version: str | None = None,
    ) -> "_ServedEngram":
        """Build an Engram from the two protocol hooks that matter.

        The memory-side twin of ``Effector.serve()``. A RECALL arrives,
        ``@on_recall`` runs, and its return value is published as the
        RECALLED hits; an IMPRINT arrives, ``@on_imprint`` runs, and its
        return value becomes the IMPRINTED receipt. What happens in
        between - an index, a vector store, an HTTP call - is your code::

            ENGRAM = Engram.serve(engram_id="notes")

            @ENGRAM.on_recall
            async def search(query, *, deadline_ms=None):
                return [Hit(id=k, entry=v) for k, v in match(query)]

            @ENGRAM.on_imprint
            async def write(op, entry, *, merge_key=None):
                return store(op, entry, merge_key)

            dendrite.attach_engram(ENGRAM)

        The result is a plain Engram: it REGISTERs under its own
        ``engram_id``, the hosting Dendrite resolves and services it
        exactly as it does a subclass, and the reply carries the Engram's
        own attribution. Lifecycle follows
        :class:`cosmonapse._hooks.LifecycleHooks` - ``@on_connect`` fires
        once when the hosting Dendrite connects it at start(),
        ``@on_schedule(every_s=N)`` loops run until stop().
        """
        return _ServedEngram(
            engram_id=engram_id,
            engram_kind=engram_kind,
            capabilities=capabilities,
            version=version,
        )


# ---------------------------------------------------------------------------
# Engram.serve() - the decorator-native form.
# ---------------------------------------------------------------------------

#: kwargs an @on_recall handler may request by declaring the parameter.
_RECALL_HANDLER_KWARGS = frozenset(
    {"filters", "context_ref", "deadline_ms", "min_confidence"}
)
#: kwargs an @on_imprint handler may request by declaring the parameter.
_IMPRINT_HANDLER_KWARGS = frozenset({"merge_key", "imprint_id", "trace_id"})


def _wanted(fn: Callable[..., Any], allowed: frozenset[str]) -> frozenset[str]:
    """Which of ``allowed`` this handler declared (``**kwargs`` takes all)."""
    wants: set[str] = set()
    try:
        for pname, p in inspect.signature(fn).parameters.items():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                return allowed
            if pname in allowed:
                wants.add(pname)
    except (ValueError, TypeError):
        pass  # no inspectable signature: positional-only call
    return frozenset(wants)


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    out = fn(*args, **kwargs)
    return await out if inspect.isawaitable(out) else out


class _ServedEngram(Engram, LifecycleHooks):
    """Concrete Engram whose read and write surfaces are decorators.

    Built by :meth:`Engram.serve`; not instantiated directly. Handlers run
    in registration order and the first non-None return answers, so a
    policy gate (an ACL, a quota, a cache) can sit in front of the real
    backend without either knowing about the other.
    """

    def __init__(
        self,
        *,
        engram_id: str,
        engram_kind: str,
        capabilities: list[str] | None,
        version: str | None,
    ) -> None:
        LifecycleHooks.__init__(self)
        self.engram_id = engram_id
        self.engram_kind = engram_kind
        self.capabilities = capabilities or []
        self.version = version
        self._recall_handlers: list[tuple[Callable[..., Any], frozenset[str]]] = []
        self._imprint_handlers: list[tuple[Callable[..., Any], frozenset[str]]] = []
        self._serve_gate: Callable[..., Any] | None = None

    # -- registration ----------------------------------------------------

    def on_recall(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register a RECALL handler; its RETURN VALUE becomes the RECALLED
        hits. The handler receives ``query``, plus ``filters`` /
        ``context_ref`` / ``deadline_ms`` / ``min_confidence`` if declared
        as keyword parameters. Return a list of :class:`Hit` (or of
        ``{"id", "entry", "score"}`` dicts), or None to fall through to the
        next handler. Sync or async."""
        self._recall_handlers.append((fn, _wanted(fn, _RECALL_HANDLER_KWARGS)))
        return fn

    def on_imprint(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register an IMPRINT handler; its RETURN VALUE becomes the
        IMPRINTED receipt. The handler receives ``op`` and ``entry``, plus
        ``merge_key`` / ``imprint_id`` / ``trace_id`` if declared as
        keyword parameters. Return an :class:`ImprintReceipt`, or the new
        entry id as a str, or None to fall through. Sync or async."""
        self._imprint_handlers.append((fn, _wanted(fn, _IMPRINT_HANDLER_KWARGS)))
        return fn

    def serves(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register the ``can_serve(query) -> bool`` gate. Optional; the
        default answers every query once a recall handler exists."""
        self._serve_gate = fn
        return fn

    # -- Engram interface -------------------------------------------------

    async def connect(self) -> None:
        """Called by the hosting Dendrite at start(): starts the
        ``@on_schedule`` loops and fires the ``@on_connect`` hooks."""
        self._launch_schedule()
        await self._fire_connect()

    async def close(self) -> None:
        """Called by the hosting Dendrite at stop()/detach: cancels the
        ``@on_schedule`` loops."""
        await self._stop_hooks()

    async def can_serve(self, query: dict[str, Any]) -> bool:
        if self._serve_gate is not None:
            return bool(await _call(self._serve_gate, query))
        return bool(self._recall_handlers)

    async def recall(
        self,
        query: dict[str, Any],
        *,
        filters: dict[str, Any] | None = None,
        context_ref: str | None = None,
        deadline_ms: int | None = None,
        min_confidence: float | None = None,
    ) -> list[Hit]:
        available = {
            "filters": filters, "context_ref": context_ref,
            "deadline_ms": deadline_ms, "min_confidence": min_confidence,
        }
        for fn, wants in self._recall_handlers:
            out = await _call(fn, query, **{k: available[k] for k in wants})
            if out is None:
                continue                      # fall through to the next
            return [
                h if isinstance(h, Hit)
                else Hit(id=str(h.get("id", "")), entry=h.get("entry") or {},
                         score=float(h.get("score", 1.0)))
                for h in out
            ]
        return []                             # a miss is not an error

    async def imprint(
        self,
        op: str,
        entry: dict[str, Any],
        *,
        merge_key: str | None = None,
        imprint_id: str | None = None,
        trace_id: str | None = None,
    ) -> ImprintReceipt:
        t0 = time.monotonic()
        available = {
            "merge_key": merge_key, "imprint_id": imprint_id,
            "trace_id": trace_id,
        }

        def receipt(eid: str | None, error: str | None = None) -> ImprintReceipt:
            return ImprintReceipt(
                engram_id=self.engram_id, op=op, id=eid,
                took_ms=int((time.monotonic() - t0) * 1000), error=error,
            )

        for fn, wants in self._imprint_handlers:
            try:
                out = await _call(fn, op, entry,
                                  **{k: available[k] for k in wants})
            except Exception as exc:  # noqa: BLE001 - a write failure is data
                logger.exception(
                    "Engram %s @on_imprint raised: %s", self.engram_id, exc,
                )
                return receipt(None, error=f"{type(exc).__name__}: {exc}")
            if out is None:
                continue                      # fall through to the next
            if isinstance(out, ImprintReceipt):
                return out
            return receipt(str(out) if out is not True else None)
        return receipt(None, error=f"unhandled imprint op {op!r}")
