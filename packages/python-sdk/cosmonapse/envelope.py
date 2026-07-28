"""
cosmonapse.envelope
~~~~~~~~~~~~~~~~~~~
Signal envelope types and codec.

Every message crossing the Synapse is a Signal - a JSON object that conforms
to this schema. The envelope carries the protocol mechanics (id, trace_id,
type, ts). The payload carries the content specific to each Signal type.

Addressing is unified under a single ``directed`` field (see :class:`Directed`):
``directed.id`` is a direct address (a neuron_id or an engram_id),
``directed.type`` is type-based routing (a neuron type or an engram_kind), and
``directed.capabilities`` is capability-based routing. Precedence on the
receiving side is id > type > capabilities.

Producer tags (who emits each type):
  [A]  Axon (skill/connector)
  [C]  Cortex (developer-built orchestrating component)

See: ENVELOPE_SPEC.md sec 7
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
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



# ---------------------------------------------------------------------------
# Ambient trace context
# ---------------------------------------------------------------------------
# The (trace_id, parent_id) of the TASK currently being handled, carried in a
# ContextVar so code that runs *inside* a task but without explicit trace
# plumbing - e.g. a ``@axon.detects_output`` hook calling
# ``dendrite.imprint`` - inherits the task's trace instead of minting a fresh
# one. Async-safe: each asyncio task sees its own binding.

_AMBIENT_TRACE: ContextVar[tuple[str, str] | None] = ContextVar(
    "cosmonapse_ambient_trace", default=None
)


def ambient_trace() -> tuple[str, str] | None:
    """Return the ambient (trace_id, parent_id) of the task being handled,
    or None when called outside any task context."""
    return _AMBIENT_TRACE.get()


@contextmanager
def trace_context(trace_id: str, parent_id: str) -> Iterator[None]:
    """Bind the ambient (trace_id, parent_id) for the duration of the block.
    Set by ``Axon.handle_task`` around the whole handling pass (neuron_fn,
    detectors, lifecycle hooks)."""
    token = _AMBIENT_TRACE.set((trace_id, parent_id))
    try:
        yield
    finally:
        _AMBIENT_TRACE.reset(token)


def new_engram_id() -> str:
    """Return a prefixed Engram entry ULID: eng_<26-char ULID>.

    Used by Engram backends for entry identifiers. See ENGRAM_DESIGN.md
    §4.6.
    """
    return f"eng_{_new_ulid()}"


def _now_utc() -> datetime:
    return datetime.now(UTC)


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

    # Interactive cognition [A] request / [C] response.
    # CLARIFICATION (above) and PERMISSION are Axon-originated requests a
    # Neuron returns as a marker; the matching *_ANSWER / *_DECISION are
    # emitted by whichever Dendrite (a central Cortex or a peer) answers.
    # There is no built-in correlation client: the developer wires the loop
    # (recall -> on miss return marker -> orchestrator imprints and/or
    # responds), keyed by parent_id == the request's id where needed.
    PERMISSION = "PERMISSION"
    PERMISSION_DECISION = "PERMISSION_DECISION"
    CLARIFICATION_ANSWER = "CLARIFICATION_ANSWER"

    # Agent management [A]
    REGISTER = "REGISTER"
    DEREGISTER = "DEREGISTER"
    HEARTBEAT = "HEARTBEAT"

    # Discovery [C]
    DISCOVER = "DISCOVER"

    # Workflow control [C]  -  cooperative cancellation of a whole trace.
    # STOP is broadcast on the trace; every Dendrite filters by trace_id,
    # cancels in-flight neuron work + engram I/O, optionally rolls back
    # Engram writes via the per-trace saga journal, then acks with STOPPED.
    STOP = "STOP"
    STOPPED = "STOPPED"


# Which types the Axon (skill) is allowed to produce
AXON_TYPES: frozenset[SignalType] = frozenset({
    SignalType.AGENT_OUTPUT,
    SignalType.CLARIFICATION,
    SignalType.PERMISSION,
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
    # Responses to Axon-originated CLARIFICATION / PERMISSION requests.
    # Emitted by the answering Dendrite (central Cortex or peer). Like
    # RECALLED/IMPRINTED they ride the synapse side of an interaction the
    # Neuron triggered; the requesting worker correlates them by parent_id
    # via its CognitionClient.
    SignalType.PERMISSION_DECISION,
    SignalType.CLARIFICATION_ANSWER,
    # Engram (see ENGRAM_DESIGN.md §4.7)  -  emitted by orchestrating
    # Dendrites on behalf of Neurons (Axons hand off via EngramClient,
    # they never publish these directly), and by Engram-hosting
    # Dendrites on the response path.
    SignalType.RECALL,
    SignalType.RECALLED,
    SignalType.IMPRINT,
    SignalType.IMPRINTED,
    # Workflow control  -  STOP is orchestrator-gated (see Dendrite
    # _ROLE_GATED_TYPES); STOPPED is the per-Dendrite ack.
    SignalType.STOP,
    SignalType.STOPPED,
})


# ---------------------------------------------------------------------------
# Directed addressing
# ---------------------------------------------------------------------------


class Directed(BaseModel):
    """Unified addressing for a Signal.

    A Signal may be addressed three ways, in precedence order:

    * ``id``           - direct address. A ``neuron_id`` for TASK-family
                         routing, or an ``engram_id`` for RECALL/IMPRINT.
    * ``type``         - type-based routing. A neuron type, or an
                         ``engram_kind``.
    * ``capabilities`` - capability-based routing.

    ``id`` wins over ``type``, which wins over ``capabilities`` on the
    receiving side. All three are optional so the same model can carry a
    pure producer-identity (``id`` only), a typed address, or a capability
    request.
    """

    model_config = {"populate_by_name": True}

    id: str | None = Field(default=None, description="Direct address (neuron_id or engram_id)")
    type: str | None = Field(default=None, description="Type-based routing (neuron type or engram_kind)")
    capabilities: list[str] = Field(default_factory=list, description="Capability-based routing")

    def is_empty(self) -> bool:
        """True when no addressing information is present at all."""
        return not self.id and not self.type and not self.capabilities


# ---------------------------------------------------------------------------
# Base envelope
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """The universal envelope for every message crossing the Synapse."""

    model_config = {"populate_by_name": True}

    v: str = Field(default="1", description="Protocol version")

    @field_validator("v")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        """Compatibility policy (ENVELOPE_SPEC §2): same-major envelopes are
        accepted (unknown payload/meta fields must be ignored by consumers);
        a different major version is rejected at decode time rather than
        half-interpreted."""
        major = v.split(".", 1)[0]
        if major != "1":
            raise ValueError(
                f"unsupported protocol version {v!r}: this SDK speaks "
                f"major version 1 (accepts '1' or '1.x')"
            )
        return v
    id: str = Field(default_factory=new_event_id, description="Unique event ID (evt_<ULID>)")
    trace_id: str = Field(default_factory=new_trace_id, description="Trace group ID (trc_<ULID>)")
    parent_id: str | None = Field(default=None, description="Parent event ID")
    type: SignalType = Field(..., description="Signal type")
    directed: Directed | None = Field(default=None, description="Unified addressing (id/type/capabilities)")
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
    def decode(cls, data: bytes | str) -> Signal:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return cls.model_validate_json(data)

    def reply(
        self,
        type: SignalType,
        payload: dict[str, Any] | None = None,
        directed: Directed | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Build a child Signal sharing this one's trace, parented to its id.

        ``directed`` propagates: when not overridden, the reply carries this
        Signal's own ``directed`` so the responder keeps the addressing
        context (e.g. echoing back which neuron produced the chain).
        """
        return Signal(
            type=type,
            trace_id=self.trace_id,
            parent_id=self.id,
            payload=payload or {},
            directed=directed if directed is not None else self.directed,
            meta=meta or {},
        )


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def directed_to(
    id: str | None = None,
    *,
    type: str | None = None,
    capabilities: list[str] | None = None,
) -> Directed:
    """Small helper for building a :class:`Directed` at call sites."""
    return Directed(id=id, type=type, capabilities=list(capabilities or []))


# ---------------------------------------------------------------------------
# Typed payload helpers
# ---------------------------------------------------------------------------


def task_signal(
    *,
    trace_id: str | None = None,
    parent_id: str | None = None,
    directed: Directed | None = None,
    input: dict[str, Any],
    context_ref: str | None = None,
    capabilities: list[str] | None = None,
    finalize: bool = False,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Dispatch work. ``finalize=True`` tags the TASK as
    terminal-handler-finalized: the worker Dendrite that runs the addressed
    (or capability-routed) Axon promotes a successful AGENT_OUTPUT reply by
    also emitting FINAL on the trace. Set automatically by
    ``Dendrite.dispatch(scope="terminal")`` so terminal-scoped Pathways
    resolve against default workers; leave False (default) when the
    dispatcher orchestrates multi-step work and owns FINAL itself."""
    payload: dict[str, Any] = {"input": input}
    if context_ref:
        payload["context_ref"] = context_ref
    if capabilities:
        payload["capabilities"] = capabilities
    if finalize:
        payload["finalize"] = True
    return Signal(
        type=SignalType.TASK,
        trace_id=trace_id or new_trace_id(),
        parent_id=parent_id,
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def agent_output_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
    output: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=SignalType.AGENT_OUTPUT,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
        payload={"output": output},
        meta=meta or {},
    )


def clarification_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def permission_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
    action: str,
    scope: dict[str, Any] | None = None,
    reason: str | None = None,
    context: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[A] A Neuron asks permission to perform ``action`` before doing it.

    Mirrors CLARIFICATION but expects a boolean verdict rather than free
    text. ``scope`` narrows the request (e.g. ``{"path": "/etc"}``);
    ``reason`` is a human-readable justification. The answering Dendrite
    replies with PERMISSION_DECISION whose ``parent_id`` is this signal's
    id.
    """
    payload: dict[str, Any] = {"action": action}
    if scope:
        payload["scope"] = scope
    if reason is not None:
        payload["reason"] = reason
    if context:
        payload["context"] = context
    return Signal(
        type=SignalType.PERMISSION,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def permission_decision_signal(
    *,
    trace_id: str,
    parent_id: str,
    granted: bool,
    directed: Directed | None = None,
    reason: str | None = None,
    ttl_ms: int | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Verdict on a PERMISSION request.

    ``parent_id`` MUST be the PERMISSION's id - that is how a consumer
    correlates the verdict to the request. ``ttl_ms`` optionally tells the
    caller how long the grant is valid (for caching in an Engram).
    """
    payload: dict[str, Any] = {"granted": bool(granted)}
    if reason is not None:
        payload["reason"] = reason
    if ttl_ms is not None:
        payload["ttl_ms"] = ttl_ms
    return Signal(
        type=SignalType.PERMISSION_DECISION,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def clarification_answer_signal(
    *,
    trace_id: str,
    parent_id: str,
    answer: Any,
    directed: Directed | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Discrete answer to a CLARIFICATION request.

    ``parent_id`` MUST be the CLARIFICATION's id. Consumers correlate by
    that parent_id (``Dendrite.await_decision`` / the
    ``on_clarification_answer`` decorator). Distinct from the
    close-the-loop flow, where the orchestrator answers by re-dispatching
    a TASK via ``respond_to_clarification`` so the asking Neuron runs
    again.
    """
    return Signal(
        type=SignalType.CLARIFICATION_ANSWER,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
        payload={"answer": answer},
        meta=meta or {},
    )


def final_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def error_signal(
    *,
    trace_id: str,
    parent_id: str | None = None,
    directed: Directed | None = None,
    code: str,
    message: str,
    recoverable: bool = False,
    meta: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=SignalType.ERROR,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
        payload={"code": code, "message": message, "recoverable": recoverable},
        meta=meta or {},
    )


def register_signal(
    *,
    directed: Directed,
    capabilities: list[str] | None = None,
    version: str | None = None,
    engram: bool = False,
    role: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[A] A participant announces itself on the Synapse.

    Both Neurons and Engrams register the same way. ``directed.id`` is the
    participant id (neuron_id or engram_id), ``directed.type`` its kind
    (``neuron_kind`` for Neurons, ``engram_kind`` for Engrams), and
    ``directed.capabilities`` its capability list.

    Every REGISTER carries one universal discriminator, ``payload.role``
    (``"neuron"`` or ``"engram"``): the single field every consumer
    (Dendrite registry, Prism, doppler) checks to classify the participant.
    ``role`` defaults from the ``engram`` flag when omitted, so callers may
    pass either. The legacy ``payload.engram = true`` marker is still
    emitted for Engrams as a back-compat alias.

    ``capabilities`` is mirrored into the payload for registry stores; when
    omitted it falls back to ``directed.capabilities``.
    """
    caps = list(capabilities) if capabilities is not None else list(directed.capabilities)
    resolved_role = role if role is not None else ("engram" if engram else "neuron")
    payload: dict[str, Any] = {"role": resolved_role, "capabilities": caps}
    if version:
        payload["version"] = version
    if engram or resolved_role == "engram":
        payload["engram"] = True
    return Signal(
        type=SignalType.REGISTER,
        trace_id=new_trace_id(),
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def deregister_signal(
    *,
    directed: Directed,
    reason: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    payload: dict[str, Any] = {}
    if reason:
        payload["reason"] = reason
    return Signal(
        type=SignalType.DEREGISTER,
        trace_id=new_trace_id(),
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def heartbeat_signal(
    *,
    directed: Directed,
    status: str = "ok",
    meta: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=SignalType.HEARTBEAT,
        trace_id=new_trace_id(),
        directed=directed,
        payload={"status": status},
        meta=meta or {},
    )


def memory_append_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
    key: str,
    value: Any,
    meta: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=SignalType.MEMORY_APPEND,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
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
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def task_awarded_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
    input: dict[str, Any],
    winning_bid: dict[str, Any] | None = None,
    context_ref: str | None = None,
    finalize: bool = False,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Award a TASK_OFFER to one bidder. The winning Axon should treat
    this exactly like a TASK: ``input`` is the work payload, ``directed``
    addresses the winner. ``winning_bid`` carries the bid the producer
    accepted (cost / eta_ms / confidence) for observability. ``finalize``
    propagates the terminal-handler-finalize tag (see ``task_signal``) into
    the TASK the winner's Dendrite synthesises."""
    payload: dict[str, Any] = {"input": input}
    if winning_bid is not None:
        payload["winning_bid"] = winning_bid
    if context_ref is not None:
        payload["context_ref"] = context_ref
    if finalize:
        payload["finalize"] = True
    return Signal(
        type=SignalType.TASK_AWARDED,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def task_declined_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
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
    """Solicit a REGISTER snapshot from peers on a namespace.

    ``neuron`` / ``capabilities`` here are *filter* fields carried in the
    payload (which participants to discover), not envelope addressing.
    """
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
    directed: Directed | None = None,
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
        directed=directed,
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
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def thought_delta_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def tool_call_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def tool_result_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def escalation_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def consensus_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def context_sync_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
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
        directed=directed,
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
    directed: Directed | None = None,
    query: dict[str, Any],
    filters: dict[str, Any] | None = None,
    context_ref: str | None = None,
    deadline_ms: int | None = None,
    min_confidence: float | None = None,
    recall_mode: str = "first",
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[D] Memory-recall request. Inherits trace_id from the containing TASK.

    Routing is addressed by default via the envelope ``directed`` field: at
    least one of ``directed.id`` (engram_id) or ``directed.type``
    (engram_kind) MUST be set. ``directed.id`` beats ``directed.type`` on
    the receiving side. ``recall_mode`` controls fan-out semantics:

    - ``"first"`` (default) - one responder wins, others drop the request.
    - ``"merge"`` - caller merges every RECALLED arriving by deadline.
    - ``"all"``   - caller treats each RECALLED as a separate stream item.
    """
    if directed is None or (not directed.id and not directed.type):
        raise ValueError(
            "recall_signal requires directed.id (engram_id) or directed.type "
            "(engram_kind); addressed routing is the default per "
            "ENGRAM_DESIGN.md S4.1"
        )
    if recall_mode not in _RECALL_MODES:
        raise ValueError(
            f"recall_mode must be one of {sorted(_RECALL_MODES)}, "
            f"got {recall_mode!r}"
        )
    payload: dict[str, Any] = {"query": query, "recall_mode": recall_mode}
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
        directed=directed,
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
    directed: Directed | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[D] Response from one Engram to a RECALL.

    ``parent_id`` MUST be the RECALL's id. ``engram_id`` in the payload
    identifies *which* Engram responded (response metadata, not routing -
    the response is correlated back to the caller by ``parent_id``).
    Multiple Engrams may emit RECALLED for the same RECALL when
    ``recall_mode`` is ``"merge"`` or ``"all"``.
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def imprint_signal(
    *,
    trace_id: str,
    parent_id: str,
    directed: Directed | None = None,
    op: str,
    entry: dict[str, Any],
    merge_key: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[D] Memory-write request. Inherits trace_id from the containing TASK.

    ``op`` is one of ``add | append | merge | upsert | delete``.
    ``merge_key`` is required when ``op`` is ``merge`` or ``upsert``.
    Addressed by default via the envelope ``directed`` field - at least one
    of ``directed.id`` (engram_id) or ``directed.type`` (engram_kind) MUST
    be set.
    """
    if op not in _IMPRINT_OPS:
        raise ValueError(
            f"imprint op must be one of {sorted(_IMPRINT_OPS)}, got {op!r}"
        )
    if directed is None or (not directed.id and not directed.type):
        raise ValueError(
            "imprint_signal requires directed.id (engram_id) or directed.type "
            "(engram_kind)"
        )
    if op in ("merge", "upsert") and not merge_key:
        raise ValueError(
            f"imprint op={op!r} requires merge_key="
        )
    payload: dict[str, Any] = {"op": op, "entry": entry}
    if merge_key is not None:
        payload["merge_key"] = merge_key
    return Signal(
        type=SignalType.IMPRINT,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
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
    directed: Directed | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[D] Receipt of a completed IMPRINT.

    ``parent_id`` MUST be the IMPRINT's id. ``engram_id`` in the payload
    identifies which Engram responded. ``id`` is the resulting Engram entry
    id when applicable (``eng_<ULID>``); absent for ``op="delete"`` of a
    non-existent key. ``error`` is set when the imprint failed but the
    Engram chose to respond rather than emit a separate ERROR.
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
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


# ---------------------------------------------------------------------------
# Workflow control helpers (STOP / STOPPED)
# ---------------------------------------------------------------------------


def stop_signal(
    *,
    trace_id: str,
    parent_id: str | None = None,
    rollback: bool = False,
    reason: str | None = None,
    directed: Directed | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Broadcast a cooperative-cancellation request for a whole trace.

    Every Dendrite filters by ``trace_id`` and self-selects: it cancels any
    in-flight neuron work and engram I/O bound to the trace, and  -  when
    ``rollback`` is set  -  replays each hosted Engram's per-trace saga
    journal in reverse before discarding it. Participants ack with
    :func:`stopped_signal` parented to this STOP's id.

    ``rollback`` only reverses *Engram* state. Side effects a Neuron caused
    through an Axon (a sent email, an external write) are not reversible
    unless that Neuron registers its own compensator.
    """
    payload: dict[str, Any] = {"rollback": bool(rollback)}
    if reason is not None:
        payload["reason"] = reason
    return Signal(
        type=SignalType.STOP,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
        payload=payload,
        meta=meta or {},
    )


def stopped_signal(
    *,
    trace_id: str,
    parent_id: str | None = None,
    node: str | None = None,
    rolled_back: bool = False,
    cancelled: int = 0,
    compensated: int = 0,
    directed: Directed | None = None,
    meta: dict[str, Any] | None = None,
) -> Signal:
    """[C] Ack from one Dendrite that it has quiesced its share of a trace.

    ``parent_id`` MUST be the STOP's id so the originator can correlate
    acks. ``node`` is an optional human label for the responding Dendrite.
    ``cancelled`` is the number of in-flight neuron tasks cancelled here;
    ``compensated`` is the number of journal inverse-ops replayed.
    """
    payload: dict[str, Any] = {
        "rolled_back": bool(rolled_back),
        "cancelled": int(cancelled),
        "compensated": int(compensated),
    }
    if node is not None:
        payload["node"] = node
    return Signal(
        type=SignalType.STOPPED,
        trace_id=trace_id,
        parent_id=parent_id,
        directed=directed,
        payload=payload,
        meta=meta or {},
    )
