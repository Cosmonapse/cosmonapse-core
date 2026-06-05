"""
cosmonapse.engram.client
~~~~~~~~~~~~~~~~~~~~~~~~
EngramClient is the caller-side bridge. The Axon and the Cortex both
call into it; only the Dendrite is allowed to touch the Synapse.

Responsibilities
----------------
* Build RECALL / IMPRINT envelopes (delegates to envelope builders).
* Publish via the hosting Dendrite's ``_publish``.
* Register pending Futures keyed by the envelope's ``id``.
* Resolve those Futures when a matching RECALLED / IMPRINTED arrives.
* Enforce ``deadline_ms`` per call.
* Cancel in-flight Futures with EngramCancelled when a TASK terminal
  event arrives on the same trace, or the Dendrite stops.

The Dendrite owns the subscription to RECALLED / IMPRINTED and calls
``EngramClient._deliver(signal)`` for every inbound. The client
matches by ``parent_id``.

This module imports the Dendrite lazily via TYPE_CHECKING to avoid an
import cycle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast, TYPE_CHECKING, Any

from cosmonapse.engram.base import (
    EngramCancelled,
    EngramNotBound,
    EngramTimeout,
    Hit,
    ImprintReceipt,
    RecallResult,
)
from cosmonapse.envelope import (
    Signal,
    SignalType,
    imprint_signal,
    recall_signal,
)

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite
    from cosmonapse.engram.base import EngramBinding

logger = logging.getLogger(__name__)


class _PendingRecall:
    __slots__ = ("future", "mode", "deadline_handle", "hits_so_far", "engrams")

    def __init__(self, future: asyncio.Future[Any], mode: str) -> None:
        self.future = future
        self.mode = mode
        self.deadline_handle: asyncio.TimerHandle | None = None
        self.hits_so_far: list[Hit] = []
        self.engrams: list[str] = []


class _PendingImprint:
    __slots__ = ("future", "deadline_handle")

    def __init__(self, future: asyncio.Future[Any]) -> None:
        self.future = future
        self.deadline_handle: asyncio.TimerHandle | None = None


class EngramClient:
    """Caller-side correlation table for Engram I/O.

    One instance per Dendrite. The Dendrite passes itself in so the
    client can call ``_publish``; the Dendrite drives ``_deliver()``
    from its inbound dispatch path.
    """

    def __init__(self, dendrite: "Dendrite") -> None:
        self._dendrite = dendrite
        self._pending_recalls: dict[str, _PendingRecall] = {}
        self._pending_imprints: dict[str, _PendingImprint] = {}
        # Group pendings by trace so terminal events can cancel them.
        self._by_trace: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Public API (used by Axon helpers and Dendrite.recall/imprint)
    # ------------------------------------------------------------------

    async def recall(
        self,
        *,
        binding: "EngramBinding | None" = None,
        engram_id: str | None = None,
        engram_kind: str | None = None,
        query: dict[str, Any],
        filters: dict[str, Any] | None = None,
        context_ref: str | None = None,
        deadline_ms: int | None = None,
        recall_mode: str | None = None,
        min_confidence: float | None = None,
        trace_id: str,
        parent_id: str,
        neuron: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> RecallResult:
        """Emit RECALL, await matching RECALLED(s) per recall_mode, return."""
        if binding is not None:
            engram_id = engram_id or binding.engram_id
            engram_kind = engram_kind or binding.engram_kind
            if deadline_ms is None:
                deadline_ms = binding.default_deadline_ms
            if recall_mode is None:
                recall_mode = binding.default_recall_mode
        if recall_mode is None:
            recall_mode = "first"

        sig = recall_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            neuron=neuron,
            engram_id=engram_id,
            engram_kind=engram_kind,
            query=query,
            filters=filters,
            context_ref=context_ref,
            deadline_ms=deadline_ms,
            min_confidence=min_confidence,
            recall_mode=recall_mode,
            meta=meta,
        )

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        pending = _PendingRecall(fut, mode=recall_mode)
        self._pending_recalls[sig.id] = pending
        self._by_trace.setdefault(trace_id, set()).add(sig.id)

        if deadline_ms is not None and deadline_ms > 0:
            pending.deadline_handle = loop.call_later(
                deadline_ms / 1000.0,
                self._on_recall_deadline,
                sig.id,
            )

        try:
            await self._dendrite._publish(sig)
        except Exception:
            self._pending_recalls.pop(sig.id, None)
            self._discard_trace_entry(trace_id, sig.id)
            if pending.deadline_handle:
                pending.deadline_handle.cancel()
            raise

        try:
            return cast(RecallResult, await fut)
        finally:
            self._pending_recalls.pop(sig.id, None)
            self._discard_trace_entry(trace_id, sig.id)
            if pending.deadline_handle:
                pending.deadline_handle.cancel()

    async def imprint(
        self,
        *,
        binding: "EngramBinding | None" = None,
        engram_id: str | None = None,
        engram_kind: str | None = None,
        op: str,
        entry: dict[str, Any],
        merge_key: str | None = None,
        await_ack: bool = False,
        deadline_ms: int | None = None,
        trace_id: str,
        parent_id: str,
        neuron: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ImprintReceipt | None:
        """Emit IMPRINT. With ``await_ack=False`` (default) return as soon as
        the envelope is on the wire. With ``await_ack=True`` await the
        matching IMPRINTED and return a receipt."""
        if binding is not None:
            engram_id = engram_id or binding.engram_id
            engram_kind = engram_kind or binding.engram_kind

        sig = imprint_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            neuron=neuron,
            engram_id=engram_id,
            engram_kind=engram_kind,
            op=op,
            entry=entry,
            merge_key=merge_key,
            meta=meta,
        )

        if not await_ack:
            await self._dendrite._publish(sig)
            return None

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        pending = _PendingImprint(fut)
        self._pending_imprints[sig.id] = pending
        self._by_trace.setdefault(trace_id, set()).add(sig.id)

        if deadline_ms is not None and deadline_ms > 0:
            pending.deadline_handle = loop.call_later(
                deadline_ms / 1000.0,
                self._on_imprint_deadline,
                sig.id,
            )

        try:
            await self._dendrite._publish(sig)
        except Exception:
            self._pending_imprints.pop(sig.id, None)
            self._discard_trace_entry(trace_id, sig.id)
            if pending.deadline_handle:
                pending.deadline_handle.cancel()
            raise

        try:
            return cast("ImprintReceipt | None", await fut)
        finally:
            self._pending_imprints.pop(sig.id, None)
            self._discard_trace_entry(trace_id, sig.id)
            if pending.deadline_handle:
                pending.deadline_handle.cancel()

    # ------------------------------------------------------------------
    # Delivery (driven by the Dendrite's inbound dispatch)
    # ------------------------------------------------------------------

    async def _deliver(self, sig: Signal) -> None:
        """Match RECALLED/IMPRINTED by parent_id and resolve pendings."""
        pid = sig.parent_id
        if pid is None:
            return
        if sig.type is SignalType.RECALLED:
            pending = self._pending_recalls.get(pid)
            if pending is None:
                return
            hits = _hits_from_payload(sig.payload.get("hits") or [])
            engram_id = sig.payload.get("engram_id") or ""
            took_ms = sig.payload.get("took_ms")
            truncated = bool(sig.payload.get("truncated"))
            if pending.mode == "first":
                if not pending.future.done():
                    pending.future.set_result(
                        RecallResult(
                            hits=hits,
                            engram_ids=(engram_id,) if engram_id else (),
                            truncated=truncated,
                            took_ms=took_ms,
                        )
                    )
            else:
                # merge / all  -> accumulate; the deadline handler resolves.
                pending.hits_so_far.extend(hits)
                if engram_id:
                    pending.engrams.append(engram_id)
        elif sig.type is SignalType.IMPRINTED:
            pending_imp = self._pending_imprints.get(pid)
            if pending_imp is None:
                return
            if not pending_imp.future.done():
                pending_imp.future.set_result(
                    ImprintReceipt(
                        engram_id=sig.payload.get("engram_id") or "",
                        op=sig.payload.get("op") or "",
                        id=sig.payload.get("id"),
                        version=sig.payload.get("version"),
                        took_ms=sig.payload.get("took_ms"),
                        error=sig.payload.get("error"),
                    )
                )

    def cancel_trace(self, trace_id: str) -> None:
        """Cancel every in-flight recall/imprint on this trace.

        Called by the Dendrite when a FINAL or ERROR on the trace
        arrives, or on shutdown."""
        ids = self._by_trace.pop(trace_id, set())
        for ev_id in ids:
            pr = self._pending_recalls.pop(ev_id, None)
            if pr is not None and not pr.future.done():
                pr.future.set_exception(EngramCancelled(
                    f"trace {trace_id} terminated while recall {ev_id} in flight"
                ))
                if pr.deadline_handle:
                    pr.deadline_handle.cancel()
            pi = self._pending_imprints.pop(ev_id, None)
            if pi is not None and not pi.future.done():
                pi.future.set_exception(EngramCancelled(
                    f"trace {trace_id} terminated while imprint {ev_id} in flight"
                ))
                if pi.deadline_handle:
                    pi.deadline_handle.cancel()

    def cancel_all(self) -> None:
        for trace_id in list(self._by_trace.keys()):
            self.cancel_trace(trace_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_recall_deadline(self, event_id: str) -> None:
        pending = self._pending_recalls.get(event_id)
        if pending is None or pending.future.done():
            return
        if pending.mode == "first":
            pending.future.set_exception(EngramTimeout(
                f"RECALL {event_id} elapsed deadline without any responder"
            ))
        else:
            # merge / all  -> resolve with whatever we have so far
            pending.future.set_result(
                RecallResult(
                    hits=sorted(pending.hits_so_far, key=lambda h: h.score, reverse=True),
                    engram_ids=tuple(pending.engrams),
                    truncated=False,
                    took_ms=None,
                )
            )

    def _on_imprint_deadline(self, event_id: str) -> None:
        pending = self._pending_imprints.get(event_id)
        if pending is None or pending.future.done():
            return
        pending.future.set_exception(EngramTimeout(
            f"IMPRINT {event_id} elapsed deadline without IMPRINTED"
        ))

    def _discard_trace_entry(self, trace_id: str, event_id: str) -> None:
        bucket = self._by_trace.get(trace_id)
        if bucket is None:
            return
        bucket.discard(event_id)
        if not bucket:
            self._by_trace.pop(trace_id, None)


def _hits_from_payload(raw_hits: list[dict[str, Any]]) -> list[Hit]:
    out: list[Hit] = []
    for h in raw_hits:
        if not isinstance(h, dict):
            continue
        out.append(Hit(
            id=str(h.get("id", "")),
            entry=cast(dict[str, Any], h.get("entry") if isinstance(h.get("entry"), dict) else {"value": h.get("entry")}),
            score=float(h.get("score", 1.0)),
        ))
    return out


# Sentinel for "no binding lookup performed"
def lookup_binding(
    bindings: dict[str, "EngramBinding"],
    name: str,
) -> "EngramBinding":
    """Strict lookup. Raises EngramNotBound when the name is unknown so a
    Neuron cannot silently hit an Engram its Axon was not wired to."""
    try:
        return bindings[name]
    except KeyError:
        raise EngramNotBound(
            f"no Engram binding named {name!r}; "
            f"available: {sorted(bindings)}"
        )
