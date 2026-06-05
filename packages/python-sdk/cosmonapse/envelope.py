"""
cosmonapse.envelope
~~~~~~~~~~~~~~~~~~~
Signal envelope types and codec.

Every message crossing the Synapse is a Signal - a JSON object that conforms
to this schema. The envelope carries the protocol mechanics (id, trace_id,
type, ts). The payload carries the content specific to each Signal type.

Producer tags (who emits each type):
  [A]  Axon (skill/connector)
  [C]  Cortex (developer-built orchestrating component)

See: ENVELOPE_SPEC.md sec 7
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator
from ulid import ULID


# ---------------------------------------------------------------------------
# ULID helpers
# ---------------------------------------------------------------------------


def _new_ulid() -> str:
    return str(ULID())


def new_event_id() -> str:
    """Return a prefixed event ULID: evt_<26-char ULID>"""
    return f"evt_{_new_ulid()}"


def new_trace_id() -> str:
    """Return a prefixed trace ULID: trc_<26-char ULID>"""
    return f"trc_{_new_ulid()}"


def new_engram_id() -> str:
    """Return a prefixed Engram entry ULID: eng_<26-char ULID>.

    Used by Engram backends for entry identifiers. See ENGRAM_DESIGN.md
    §4.6.
    """
    return f"eng_{_new_ulid()}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------


class SignalType(str, Enum):
    # Lifecycle [A] / [C]
    TASK = "TASK"
    AGENT_OUTPUT = "AGENT_OUTPUT"
    FINAL = "FINAL"
    ERROR = "ERROR"

    # Routing [C]
    TASK_OFFER = "TASK_OFFER"
    BID = "BID"
    TASK_AWARDED = "TASK_AWARDED"
    TASK_DECLINED = "TASK_DECLINED"

    # Cognition [C]
    THOUGHT_DELTA = "THOUGHT_DELTA"
    PLAN = "PLAN"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"

    # Memory [C]
    MEMORY_APPEND = "MEMORY_APPEND"
    ESCALATION = "ESCALATION"

    # Engram [C]   -  see ENGRAM_DESIGN.md
    RECALL = "RECALL"
    RECALLED = "RECALLED"
    IMPRINT = "IMPRINT"
    IMPRINTED = "IMPRINTED"

    # Coordination [C] / [A]
    CONSENSUS = "CONSENSUS"
    CONTEXT_SYNC = "CONTEXT_SYNC"
    CRITIQUE = "CRITIQUE"
    CLARIFICATION = "CLARIFICATION"

    # Agent management [A]
    REGISTER = "REGISTER"
    DEREGISTER = "DEREGISTER"
    HEARTBEAT = "HEARTBEAT"

    # Discovery [C]
    DISCOVER = "DISCOVER"


# Which types the Axon (skill) is allowed to produce
AXON_TYPES: frozenset[SignalType] = frozenset({
    SignalType.AGENT_OUTPUT,
    SignalType.CLARIFICATION,
    SignalType.ERROR,
    SignalType.REGISTER,
    SignalType.DEREGISTER,
    SignalType.HEARTBEAT,
})

# Which types the Cortex (orchestrator/orchestrator) is allowed to produce
SYNAPSE_TYPES: frozenset[SignalType] = frozenset({
    SignalType.TASK,
    SignalType.FINAL,
    SignalType.ERROR,
    SignalType.TASK_OFFER,
    SignalType.BID,
    SignalType.TASK_AWARDED,
    SignalType.TASK_DECLINED,
    SignalType.THOUGHT_DELTA,
    SignalType.PLAN,
    SignalType.TOOL_CALL,
    SignalType.TOOL_RESULT,
    SignalType.MEMORY_APPEND,
    SignalType.ESCALATION,
    SignalType.CONSENSUS,
    SignalType.CONTEXT_SYNC,
    SignalType.CRITIQUE,
    SignalType.DISCOVER,
    # Engram (see ENGRAM_DESIGN.md §4.7)  -  emitted by orchestrating
    # Dendrites on behalf of Neurons (Axons hand off via EngramClient,
    # they never publish these directly), and by Engram-hosting
    # Dendrites on the response path.
    SignalType.RECALL,
    SignalType.RECALLED,
    SignalType.IMPRINT,
    SignalType.IMPRINTED,
})


# ---------------------------------------------------------------------------
# Base envelope
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """The universal envelope for every message crossing the Synapse."""

    model_config = {"populate_by_name": True}

    v: str = Field(default="1", description="Protocol version")
    id: str = Field(default_factory=new_event_id, description="Unique event ID (evt_<ULID>)")
    trace_id: str = Field(default_factory=new_trace_id, description="Trace group ID (trc_<ULID>)")
    parent_id: str | None = Field(default=None, description="Parent event ID")
    type: SignalType = Field(..., description="Signal type")
    neuron: str | None = Field(default=None, description="Neuron identifier")
    ts: datetime = Field(default_factory=_now_utc, description="UTC emission timestamp")
    payload: dict[str, Any] = Field(default_factory=dict, description="Type-specific payload")
    meta: dict[str, Any] = Field(default_factory=dict, description="Non-semantic annotations")

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v.startswith("evt_"):
            raise ValueError(f"Signal id must start with 'evt_', got: {v!r}")
        return v

    @field_validator("trace_id")
    @classmethod
    def _validate_trace_id(cls, v: str) -> str:
        if not v.startswith("trc_"):
            raise ValueError(f"trace_id must start with 'trc_', got: {v!r}")
        return v

    @field_validator("parent_id")
    @classmethod
    def _validate_parent_id(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("evt_"):
            raise ValueError(f"parent_id must start with 'evt_', got: {v!r}")
        return v

    def encode(self) -> bytes:
        return self.model_dump_json(exclude_none=False).encode("utf-8")

    @classmethod
    def decode(cls, data: bytes | str) -> "Signal":
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return cls.model_validate_json(data)

    def reply(
        self,
        type: SignalType,
        payload: dict[str, Any] | None = None,
        neuron: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "Signal":
        return Signal(
            type=type,
            trace_id=self.trace_id,
            parent_id=self.id,
            payload=payload or {},
            neuron=neuron or self.neuron,
            meta=meta or {},
        )


# ---------------------------------------------------------------------------
# Typed payload helpers
# ---------------------------------------------------------------------------


def task_signal(
    *,
    trace_id: str | None = None,
    parent_id: str | None = None,
    neuron: str | None = None,
    input: dict[str, Any],
    context_ref: str | None = None,
    capabilities: list[str] | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    payload: dict[str, Any] = {"input": input}
    if context_ref:
        payload["context_ref"] = context_ref
    if capabilities:
        payload["capabilities"] = capabilities
    return Signal(
        type=SignalType.TASK,
        trace_id=trace_id or new_trace_id(),
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def agent_output_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str,
    output: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=SignalType.AGENT_OUTPUT,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload={"output": output},
        meta=meta or {},
    )


def clarification_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str,
    question: str,
    context: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    payload: dict[str, Any] = {"question": question}
    if context:
        payload["context"] = context
    return Signal(
        type=SignalType.CLARIFICATION,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def final_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    result: dict[str, Any],
    cost: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    payload: dict[str, Any] = {"result": result}
    if cost:
        payload["cost"] = cost
    return Signal(
        type=SignalType.FINAL,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def error_signal(
    *,
    trace_id: str,
    parent_id: str | None = None,
    neuron: str | None = None,
    code: str,
    message: str,
    recoverable: bool = False,
    meta: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=SignalType.ERROR,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload={"code": code, "message": message, "recoverable": recoverable},
        meta=meta or {},
    )


def register_signal(
    *,
    neuron: str,
    capabilities: list[str],
    version: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    payload: dict[str, Any] = {"capabilities": capabilities}
    if version:
        payload["version"] = version
    return Signal(
        type=SignalType.REGISTER,
        trace_id=new_trace_id(),
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def deregister_signal(
    *,
    neuron: str,
    reason: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    payload: dict[str, Any] = {}
    if reason:
        payload["reason"] = reason
    return Signal(
        type=SignalType.DEREGISTER,
        trace_id=new_trace_id(),
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def heartbeat_signal(
    *,
    neuron: str,
    status: str = "ok",
    meta: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=SignalType.HEARTBEAT,
        trace_id=new_trace_id(),
        neuron=neuron,
        payload={"status": status},
        meta=meta or {},
    )


def memory_append_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    key: str,
    value: Any,
    meta: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=SignalType.MEMORY_APPEND,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload={"key": key, "value": value},
        meta=meta or {},
    )


def task_offer_signal(
    *,
    trace_id: str,
    parent_id: str | None = None,
    input: dict[str, Any],
    capabilities: list[str] | None = None,
    deadline_ms: int | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    payload: dict[str, Any] = {"input": input}
    if capabilities:
        payload["capabilities"] = capabilities
    if deadline_ms:
        payload["deadline_ms"] = deadline_ms
    return Signal(
        type=SignalType.TASK_OFFER,
        trace_id=trace_id,
        parent_id=parent_id,
        payload=payload,
        meta=meta or {},
    )


def bid_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str,
    cost: float,
    eta_ms: int | None = None,
    confidence: float | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    payload: dict[str, Any] = {"cost": cost}
    if eta_ms is not None:
        payload["eta_ms"] = eta_ms
    if confidence is not None:
        payload["confidence"] = confidence
    return Signal(
        type=SignalType.BID,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def task_awarded_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str,
    input: dict[str, Any],
    winning_bid: dict[str, Any] | None = None,
    context_ref: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Award a TASK_OFFER to one bidder. The winning Axon should treat
    this exactly like a TASK: ``input`` is the work payload, ``neuron``
    is the addressee. ``winning_bid`` carries the bid the producer
    accepted (cost / eta_ms / confidence) for observability."""
    payload: dict[str, Any] = {"input": input}
    if winning_bid is not None:
        payload["winning_bid"] = winning_bid
    if context_ref is not None:
        payload["context_ref"] = context_ref
    return Signal(
        type=SignalType.TASK_AWARDED,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def task_declined_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    reason: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C/A] Decline a TASK_OFFER. Producers emit this for losing bidders
    after picking a winner (informational); workers may emit it
    proactively to signal they will not bid on this offer."""
    payload: dict[str, Any] = {}
    if reason is not None:
        payload["reason"] = reason
    return Signal(
        type=SignalType.TASK_DECLINED,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def discover_signal(
    *,
    neuron: str | None = None,
    capabilities: list[str] | None = None,
    trace_id: str | None = None,
    parent_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """Solicit a REGISTER snapshot from peers on a namespace."""
    payload: dict[str, Any] = {}
    if neuron is not None:
        payload["neuron"] = neuron
    if capabilities is not None:
        payload["capabilities"] = list(capabilities)
    return Signal(
        type=SignalType.DISCOVER,
        trace_id=trace_id or new_trace_id(),
        parent_id=parent_id,
        payload=payload,
        meta=meta or {},
    )


def critique_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    target_event_id: str,
    issues: list[dict[str, Any]],
    verdict: str,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """Peer review of another Neuron's output."""
    return Signal(
        type=SignalType.CRITIQUE,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload={
            "target_event_id": target_event_id,
            "issues": issues,
            "verdict": verdict,
        },
        meta=meta or {},
    )


def plan_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    steps: list[dict[str, Any]],
    rationale: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """Structured plan emitted before execution."""
    payload: dict[str, Any] = {"steps": steps}
    if rationale is not None:
        payload["rationale"] = rationale
    return Signal(
        type=SignalType.PLAN,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def thought_delta_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    delta: str,
    seq: int | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """Streaming reasoning chunk."""
    payload: dict[str, Any] = {"delta": delta}
    if seq is not None:
        payload["seq"] = seq
    return Signal(
        type=SignalType.THOUGHT_DELTA,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def tool_call_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    tool: str,
    args: dict[str, Any],
    call_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """Neuron invoking an external tool."""
    payload: dict[str, Any] = {"tool": tool, "args": args}
    if call_id is not None:
        payload["call_id"] = call_id
    return Signal(
        type=SignalType.TOOL_CALL,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def tool_result_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    tool: str,
    result: Any = None,
    error: str | None = None,
    call_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """Result returned from a tool. Exactly one of result/error should be set."""
    payload: dict[str, Any] = {"tool": tool}
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    if call_id is not None:
        payload["call_id"] = call_id
    return Signal(
        type=SignalType.TOOL_RESULT,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def escalation_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    reason: str,
    target: str | None = None,
    context: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Escalate a task or sub-decision to a higher authority Neuron."""
    payload: dict[str, Any] = {"reason": reason}
    if target is not None:
        payload["target"] = target
    if context is not None:
        payload["context"] = context
    return Signal(
        type=SignalType.ESCALATION,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def consensus_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    members: list[str],
    verdict: str,
    votes: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Record a consensus outcome among multiple Neurons."""
    payload: dict[str, Any] = {"members": members, "verdict": verdict}
    if votes is not None:
        payload["votes"] = votes
    return Signal(
        type=SignalType.CONSENSUS,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def context_sync_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    snapshot: dict[str, Any],
    version: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Share/synchronise Engram context across Neurons."""
    payload: dict[str, Any] = {"snapshot": snapshot}
    if version is not None:
        payload["version"] = version
    return Signal(
        type=SignalType.CONTEXT_SYNC,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


# ---------------------------------------------------------------------------
# Engram signal builders (see ENGRAM_DESIGN.md S4)
# ---------------------------------------------------------------------------


_RECALL_MODES: frozenset[str] = frozenset({"first", "merge", "all"})
_IMPRINT_OPS: frozenset[str] = frozenset({
    "add", "append", "merge", "upsert", "delete",
})


def recall_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    engram_id: str | None = None,
    engram_kind: str | None = None,
    query: dict[str, Any],
    filters: dict[str, Any] | None = None,
    context_ref: str | None = None,
    deadline_ms: int | None = None,
    min_confidence: float | None = None,
    recall_mode: str = "first",
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[D] Memory-recall request. Inherits trace_id from the containing TASK.

    Routing is addressed by default: at least one of ``engram_id`` or
    ``engram_kind`` MUST be set. ``engram_id`` beats ``engram_kind`` on
    the receiving side. ``recall_mode`` controls fan-out semantics:

    - ``"first"`` (default) - one responder wins, others drop the request.
    - ``"merge"`` - caller merges every RECALLED arriving by deadline.
    - ``"all"``   - caller treats each RECALLED as a separate stream item.
    """
    if not engram_id and not engram_kind:
        raise ValueError(
            "recall_signal requires engram_id= or engram_kind= (or both); "
            "addressed routing is the default per ENGRAM_DESIGN.md S4.1"
        )
    if recall_mode not in _RECALL_MODES:
        raise ValueError(
            f"recall_mode must be one of {sorted(_RECALL_MODES)}, "
            f"got {recall_mode!r}"
        )
    payload: dict[str, Any] = {"query": query, "recall_mode": recall_mode}
    if engram_id is not None:
        payload["engram_id"] = engram_id
    if engram_kind is not None:
        payload["engram_kind"] = engram_kind
    if filters is not None:
        payload["filters"] = filters
    if context_ref is not None:
        payload["context_ref"] = context_ref
    if deadline_ms is not None:
        payload["deadline_ms"] = deadline_ms
    if min_confidence is not None:
        payload["min_confidence"] = min_confidence
    return Signal(
        type=SignalType.RECALL,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def recalled_signal(
    *,
    trace_id: str,
    parent_id: str,
    engram_id: str,
    hits: list[dict[str, Any]],
    truncated: bool = False,
    took_ms: int | None = None,
    neuron: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[D] Response from one Engram to a RECALL.

    ``parent_id`` MUST be the RECALL's id. Multiple Engrams may emit
    RECALLED for the same RECALL when ``recall_mode`` is ``"merge"`` or
    ``"all"``.
    """
    payload: dict[str, Any] = {
        "engram_id": engram_id,
        "hits": list(hits),
        "truncated": bool(truncated),
    }
    if took_ms is not None:
        payload["took_ms"] = took_ms
    return Signal(
        type=SignalType.RECALLED,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def imprint_signal(
    *,
    trace_id: str,
    parent_id: str,
    neuron: str | None = None,
    engram_id: str | None = None,
    engram_kind: str | None = None,
    op: str,
    entry: dict[str, Any],
    merge_key: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[D] Memory-write request. Inherits trace_id from the containing TASK.

    ``op`` is one of ``add | append | merge | upsert | delete``.
    ``merge_key`` is required when ``op`` is ``merge`` or ``upsert``.
    Addressed by default - at least one of ``engram_id`` or ``engram_kind``
    MUST be set.
    """
    if op not in _IMPRINT_OPS:
        raise ValueError(
            f"imprint op must be one of {sorted(_IMPRINT_OPS)}, got {op!r}"
        )
    if not engram_id and not engram_kind:
        raise ValueError(
            "imprint_signal requires engram_id= or engram_kind= (or both)"
        )
    if op in ("merge", "upsert") and not merge_key:
        raise ValueError(
            f"imprint op={op!r} requires merge_key="
        )
    payload: dict[str, Any] = {"op": op, "entry": entry}
    if engram_id is not None:
        payload["engram_id"] = engram_id
    if engram_kind is not None:
        payload["engram_kind"] = engram_kind
    if merge_key is not None:
        payload["merge_key"] = merge_key
    return Signal(
        type=SignalType.IMPRINT,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )


def imprinted_signal(
    *,
    trace_id: str,
    parent_id: str,
    engram_id: str,
    op: str,
    id: str | None = None,
    version: int | None = None,
    took_ms: int | None = None,
    error: str | None = None,
    neuron: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[D] Receipt of a completed IMPRINT.

    ``parent_id`` MUST be the IMPRINT's id. ``id`` is the resulting Engram
    entry id when applicable (``eng_<ULID>``); absent for ``op="delete"``
    of a non-existent key. ``error`` is set when the imprint failed but
    the Engram chose to respond rather than emit a separate ERROR.
    """
    payload: dict[str, Any] = {"engram_id": engram_id, "op": op}
    if id is not None:
        payload["id"] = id
    if version is not None:
        payload["version"] = version
    if took_ms is not None:
        payload["took_ms"] = took_ms
    if error is not None:
        payload["error"] = error
    return Signal(
        type=SignalType.IMPRINTED,
        trace_id=trace_id,
        parent_id=parent_id,
        neuron=neuron,
        payload=payload,
        meta=meta or {},
    )
