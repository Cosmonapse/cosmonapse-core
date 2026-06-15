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
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)


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
        journal: dict[str, list[dict[str, Any]]] = getattr(
            self, "_saga_journal", None
        )
        if journal is None:
            journal = {}
            self._saga_journal = journal  # type: ignore[attr-defined]
        journal.setdefault(trace_id, []).append(
            {"op": op, "entry": entry, "merge_key": merge_key}
        )

    async def compensate(self, trace_id: str) -> int:
        """Reverse every journaled write for ``trace_id`` and discard the
        journal. Returns the number of inverse ops applied.

        Replays in reverse (LIFO) so nested overwrites unwind to the
        original state. Best-effort: a failing inverse is logged and the
        rest still run. Only Engram state is reversed - external side
        effects are out of scope (see :func:`cosmonapse.envelope.stop_signal`)."""
        journal: dict[str, list[dict[str, Any]]] | None = getattr(
            self, "_saga_journal", None
        )
        if not journal:
            return 0
        inverses = journal.pop(trace_id, [])
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
        journal: dict[str, list[dict[str, Any]]] | None = getattr(
            self, "_saga_journal", None
        )
        if journal:
            journal.pop(trace_id, None)

    # ------------------------------------------------------------------
    # Optional capability negotiation
    # ------------------------------------------------------------------

    async def can_serve(self, query: dict[str, Any]) -> bool:
        """Return False if this Engram cannot satisfy the query (e.g. a
        BM25 engram asked for vector search). The hosting Dendrite skips
        responding when this returns False. Default: serve everything."""
        return True