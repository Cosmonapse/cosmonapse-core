"""
cosmonapse.effector.client
~~~~~~~~~~~~~~~~~~~~~~~~~~
EffectorClient is the caller-side bridge for tool I/O - the action-side
twin of :class:`cosmonapse.engram.client.EngramClient`, built the way
that module's docstring promised any request/reply client could be.
The Axon (native tool calls, injected ``call_tool`` helper) and
orchestrating Dendrites both call into it; only the Dendrite is allowed
to touch the Synapse.

It is a *thin wrapper over a per-operation Pathway*. All correlation,
buffering, deadline, and cancellation machinery lives in the Pathway /
Dendrite, not here:

* Build a TOOL_CALL envelope (delegates to the envelope builder).
* Open an op-Pathway keyed on that envelope's ``id`` via
  ``Dendrite._open_op_pathway`` - the Dendrite routes the matching
  TOOL_RESULT back to it by ``parent_id``.
* Publish via the hosting Dendrite's ``_publish``.
* ``await`` the TOOL_RESULT off the Pathway and shape a ToolOutcome.
* Map a deadline timeout to :class:`EffectorTimeout` and a Pathway
  closed by the parent TASK's terminal event (or Dendrite shutdown) to
  :class:`EffectorCancelled`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from cosmonapse.effector.base import (
    EffectorCancelled,
    EffectorTimeout,
    ToolOutcome,
)
from cosmonapse.envelope import Directed, Signal, SignalType, tool_call_signal
from cosmonapse.pathway import PathwayClosedError

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite
    from cosmonapse.effector.base import EffectorBinding

logger = logging.getLogger(__name__)


class EffectorClient:
    """Caller-side helper for tool I/O - a thin wrapper over op-Pathways.

    One instance per Dendrite. The Dendrite passes itself in so the
    client can open op-Pathways (``_open_op_pathway``) and publish
    (``_publish``). Correlation, deadlines, and cancellation are the
    Pathway's job.
    """

    def __init__(self, dendrite: Dendrite) -> None:
        self._dendrite = dendrite

    async def call(
        self,
        *,
        binding: EffectorBinding | None = None,
        effector_id: str | None = None,
        effector_kind: str | None = None,
        tool: str,
        args: dict[str, Any] | None = None,
        call_id: str | None = None,
        deadline_ms: int | None = None,
        trace_id: str,
        parent_id: str,
        neuron: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ToolOutcome:
        """Emit TOOL_CALL, await the matching TOOL_RESULT, return.

        ``neuron`` is accepted for caller observability; it is not part
        of the envelope addressing (a TOOL_CALL's ``directed`` addresses
        the target Effector, not the producer). With no ``deadline_ms``
        (and none on the binding) the call waits until the trace
        terminates - callers that must not hang pass a deadline.
        """
        if binding is not None:
            effector_id = effector_id or binding.directed_id
            effector_kind = effector_kind or binding.directed_type
            if deadline_ms is None:
                deadline_ms = binding.default_deadline_ms

        sig = tool_call_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=effector_id, type=effector_kind),
            tool=tool,
            args=args or {},
            call_id=call_id,
            meta=meta or {},
        )

        # Open the op-Pathway BEFORE publishing so an inline (in-memory)
        # TOOL_RESULT is buffered, never lost. The Dendrite routes
        # TOOL_RESULT by parent_id == sig.id back to this Pathway.
        pw = self._dendrite._open_op_pathway(op_id=sig.id, trace_id=trace_id)
        deadline_s = (deadline_ms / 1000.0) if deadline_ms else None
        try:
            await self._dendrite._publish(sig)
            try:
                recv = await pw.wait_for(
                    SignalType.TOOL_RESULT, timeout_s=deadline_s,
                )
            except asyncio.TimeoutError:
                raise EffectorTimeout(
                    "TOOL_CALL elapsed its deadline without TOOL_RESULT"
                ) from None
            except PathwayClosedError:
                raise EffectorCancelled(
                    "trace terminated while TOOL_CALL was in flight"
                ) from None
            return _outcome_from(recv, fallback_tool=tool)
        finally:
            await pw.close()


def _outcome_from(sig: Signal, *, fallback_tool: str) -> ToolOutcome:
    """Shape a TOOL_RESULT Signal into a ToolOutcome. The answering
    Effector is read off the reply's ``directed`` attribution."""
    d = sig.directed
    return ToolOutcome(
        tool=sig.payload.get("tool") or fallback_tool,
        result=sig.payload.get("result"),
        error=sig.payload.get("error"),
        call_id=sig.payload.get("call_id"),
        took_ms=sig.payload.get("took_ms"),
        effector_id=(d.id if d else None),
    )
