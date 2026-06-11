"""
cosmonapse.engram.client
~~~~~~~~~~~~~~~~~~~~~~~~
EngramClient is the caller-side bridge for Engram I/O. The Axon and the
Cortex both call into it; only the Dendrite is allowed to touch the Synapse.

It is a *thin wrapper over a per-operation Pathway*. All correlation,
buffering, deadline, and cancellation machinery lives in the Pathway /
Dendrite, not here:

* Build a RECALL / IMPRINT envelope (delegates to the envelope builders).
* Open an op-Pathway keyed on that envelope's ``id`` via
  ``Dendrite._open_op_pathway`` - the Dendrite routes the matching
  RECALLED / IMPRINTED back to it by ``parent_id``.
* Publish via the hosting Dendrite's ``_publish``.
* ``await`` the response off the Pathway (``wait_for`` for a single
  responder, an iterate-until-deadline loop for ``merge`` / ``all``).
* Map a deadline timeout to :class:`EngramTimeout` and a Pathway closed by
  the parent TASK's terminal event (or Dendrite shutdown) to
  :class:`EngramCancelled`.

Because correlation is per-operation (``parent_id``) and lives in the
generic Pathway primitive, any future request/reply client can be built the
same way. This module imports the Dendrite lazily via TYPE_CHECKING to avoid
an import cycle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from cosmonapse.engram.base import (
    EngramCancelled,
    EngramNotBound,
    EngramTimeout,
    Hit,
    ImprintReceipt,
    RecallResult,
)
from cosmonapse.envelope import (
    Directed,
    Signal,
    SignalType,
    imprint_signal,
    recall_signal,
)
from cosmonapse.pathway import Pathway, PathwayClosedError

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite
    from cosmonapse.engram.base import EngramBinding

logger = logging.getLogger(__name__)


class EngramClient:
    """Caller-side helper for Engram I/O - a thin wrapper over op-Pathways.

    One instance per Dendrite. The Dendrite passes itself in so the client
    can open op-Pathways (``_open_op_pathway``) and publish (``_publish``).
    Correlation, deadlines, and cancellation are the Pathway's job.
    """

    def __init__(self, dendrite: "Dendrite") -> None:
        self._dendrite = dendrite

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
        """Emit RECALL, await matching RECALLED(s) per recall_mode, return.

        ``neuron`` is accepted for caller observability; it is not part of
        the envelope addressing (a RECALL's ``directed`` addresses the
        target Engram, not the producer).
        """
        if binding is not None:
            engram_id = engram_id or binding.directed_id
            engram_kind = engram_kind or binding.directed_type
            if deadline_ms is None:
                deadline_ms = binding.default_deadline_ms
            if recall_mode is None:
                recall_mode = binding.default_recall_mode
        if recall_mode is None:
            recall_mode = "first"

        sig = recall_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=engram_id, type=engram_kind),
            query=query,
            filters=filters,
            context_ref=context_ref,
            deadline_ms=deadline_ms,
            min_confidence=min_confidence,
            recall_mode=recall_mode,
            meta=meta,
        )

        # Open the op-Pathway BEFORE publishing so an inline (in-memory)
        # RECALLED is buffered, never lost. The Dendrite routes RECALLED by
        # parent_id == sig.id back to this Pathway.
        pw = self._dendrite._open_op_pathway(op_id=sig.id, trace_id=trace_id)
        deadline_s = (deadline_ms / 1000.0) if deadline_ms else None
        try:
            await self._dendrite._publish(sig)
            if recall_mode == "first":
                return await self._await_first_recalled(pw, deadline_s)
            return await self._collect_recalled(pw, deadline_s)
        finally:
            await pw.close()

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
        matching IMPRINTED and return a receipt.

        ``neuron`` is accepted for caller observability; it is not part of
        the envelope addressing (an IMPRINT's ``directed`` addresses the
        target Engram, not the producer).
        """
        if binding is not None:
            engram_id = engram_id or binding.directed_id
            engram_kind = engram_kind or binding.directed_type

        sig = imprint_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=engram_id, type=engram_kind),
            op=op,
            entry=entry,
            merge_key=merge_key,
            meta=meta,
        )

        if not await_ack:
            await self._dendrite._publish(sig)
            return None

        pw = self._dendrite._open_op_pathway(op_id=sig.id, trace_id=trace_id)
        deadline_s = (deadline_ms / 1000.0) if deadline_ms else None
        try:
            await self._dendrite._publish(sig)
            try:
                recv = await pw.wait_for(
                    SignalType.IMPRINTED, timeout_s=deadline_s,
                )
            except asyncio.TimeoutError:
                raise EngramTimeout(
                    "IMPRINT elapsed its deadline without IMPRINTED"
                ) from None
            except PathwayClosedError:
                raise EngramCancelled(
                    "trace terminated while IMPRINT was in flight"
                ) from None
            return ImprintReceipt(
                engram_id=recv.payload.get("engram_id") or "",
                op=recv.payload.get("op") or "",
                id=recv.payload.get("id"),
                version=recv.payload.get("version"),
                took_ms=recv.payload.get("took_ms"),
                error=recv.payload.get("error"),
            )
        finally:
            await pw.close()

    # ------------------------------------------------------------------
    # Response shaping off the op-Pathway
    # ------------------------------------------------------------------

    async def _await_first_recalled(
        self, pw: Pathway, deadline_s: float | None,
    ) -> RecallResult:
        """recall_mode='first': resolve on the first RECALLED."""
        try:
            sig = await pw.wait_for(SignalType.RECALLED, timeout_s=deadline_s)
        except asyncio.TimeoutError:
            raise EngramTimeout(
                "RECALL elapsed its deadline without any responder"
            ) from None
        except PathwayClosedError:
            raise EngramCancelled(
                "trace terminated while RECALL was in flight"
            ) from None
        return _recall_result_from(sig)

    async def _collect_recalled(
        self, pw: Pathway, deadline_s: float | None,
    ) -> RecallResult:
        """recall_mode='merge'/'all': accumulate hits across every responder
        until the deadline elapses, then return them merged and score-sorted.
        Without a deadline the loop runs until the trace is cancelled (which
        surfaces as EngramCancelled) - matching the legacy behaviour where
        merge/all relied on a deadline to resolve."""
        hits: list[Hit] = []
        engrams: list[str] = []
        loop = asyncio.get_running_loop()
        end = (loop.time() + deadline_s) if deadline_s is not None else None
        while True:
            remaining = None if end is None else end - loop.time()
            if remaining is not None and remaining <= 0:
                break
            try:
                sig = await pw.wait_for(
                    SignalType.RECALLED, timeout_s=remaining,
                )
            except asyncio.TimeoutError:
                break
            except PathwayClosedError:
                raise EngramCancelled(
                    "trace terminated while RECALL was in flight"
                ) from None
            hits.extend(_hits_from_payload(sig.payload.get("hits") or []))
            eid = sig.payload.get("engram_id")
            if eid:
                engrams.append(eid)
        return RecallResult(
            hits=sorted(hits, key=lambda h: h.score, reverse=True),
            engram_ids=tuple(engrams),
            truncated=False,
            took_ms=None,
        )


def _recall_result_from(sig: Signal) -> RecallResult:
    """Shape a single RECALLED Signal into a RecallResult."""
    engram_id = sig.payload.get("engram_id") or ""
    return RecallResult(
        hits=_hits_from_payload(sig.payload.get("hits") or []),
        engram_ids=(engram_id,) if engram_id else (),
        truncated=bool(sig.payload.get("truncated")),
        took_ms=sig.payload.get("took_ms"),
    )


def _hits_from_payload(raw_hits: list[dict[str, Any]]) -> list[Hit]:
    out: list[Hit] = []
    for h in raw_hits:
        if not isinstance(h, dict):
            continue
        out.append(Hit(
            id=str(h.get("id", "")),
            entry=h.get("entry") if isinstance(h.get("entry"), dict) else {"value": h.get("entry")},
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
