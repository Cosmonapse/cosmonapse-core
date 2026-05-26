"""
cosmonapse.envelope
~~~~~~~~~~~~~~~~~~~
Signal envelope types and codec.

Every message crossing the Synapse is a Signal — a JSON object that conforms
to this schema. The envelope carries the protocol mechanics (id, trace_id,
type, ts). The payload carries the content specific to each Signal type.

Producer tags (who emits each type):
  [A]  Axon (skill/connector)
  [C]  Cortex (developer-built orchestrating component)

See: ENVELOPE_SPEC.md §7
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------


class SignalType(str, Enum):
    # Lifecycle [A] / [C]
    TASK = "TASK"                      # [C] Dispatch work to a Neuron
    AGENT_OUTPUT = "AGENT_OUTPUT"      # [A] Neuron returned a result (neutral)
    FINAL = "FINAL"                    # [C] Workflow concluded
    ERROR = "ERROR"                    # [A][C] Something went wrong

    # Routing [C]
    TASK_OFFER = "TASK_OFFER"          # [C] Broadcast task to candidate Neurons
    BID = "BID"                        # [C] Cortex bids on behalf of a Neuron
    TASK_AWARDED = "TASK_AWARDED"      # [C] Task assigned to winning Neuron
    TASK_DECLINED = "TASK_DECLINED"    # [C] Cortex declines a task offer

    # Cognition [C]
    THOUGHT_DELTA = "THOUGHT_DELTA"    # [C] Streaming reasoning chunk
    PLAN = "PLAN"                      # [C] Structured plan before execution
    TOOL_CALL = "TOOL_CALL"           # [C] Neuron invoking an external tool
    TOOL_RESULT = "TOOL_RESULT"       # [C] Result returned from a tool

    # Memory [C]
    MEMORY_APPEND = "MEMORY_APPEND"   # [C] Write to shared Engram
    ESCALATION = "ESCALATION"         # [C] Task escalated to higher authority

    # Coordination [C] / [A]
    CONSENSUS = "CONSENSUS"            # [C] Multi-Neuron agreement reached
    CONTEXT_SYNC = "CONTEXT_SYNC"     # [C] Engram sync across Neurons
    CRITIQUE = "CRITIQUE"             # [C] Peer review of another Neuron's output
    CLARIFICATION = "CLARIFICATION"   # [A] Neuron needs more information

    # Agent management [A]
    REGISTER = "REGISTER"             # [A] Neuron connected to Synapse
    DEREGISTER = "DEREGISTER"         # [A] Neuron disconnecting
    HEARTBEAT = "HEARTBEAT"           # [A] Liveness signal


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
})


# ---------------------------------------------------------------------------
# Base envelope
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """
    The universal envelope for every message crossing the Synapse.

    Fields
    ------
    v          Protocol version. Always "1" for this release.
    id         Unique event ID. Format: evt_<26-char ULID>.
    trace_id   Groups all Signals belonging to one logical workflow.
               Format: trc_<26-char ULID>.
    parent_id  The id of the Signal that caused this one. Optional.
    type       One of the SignalType enum values.
    neuron     Identifier of the Neuron that produced this Signal.
               Required for Axon-produced types; optional for Cortex types.
    ts         UTC timestamp of emission.
    payload    Type-specific content. Arbitrary JSON object.
    meta       Non-semantic annotations: model name, token counts, cost, etc.
    """

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
        """Serialise to UTF-8 JSON bytes for wire transmission."""
        return self.model_dump_json(exclude_none=False).encode("utf-8")

    @classmethod
    def decode(cls, data: bytes | str) -> "Signal":
        """Deserialise from JSON bytes or string."""
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
        """
        Construct a reply Signal that shares this Signal's trace_id
        and sets parent_id to this Signal's id.
        """
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
# These are convenience constructors — not required by the protocol.
# The protocol only requires a valid Signal with the correct type and payload.


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
    """
    [C] Dispatch a unit of work to a Neuron.

    payload.input         — the task data the Neuron receives
    payload.context_ref   — optional Engram reference; Axon fetches embeddings
    payload.capabilities  — optional capability hints for routing
    """
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
    """
    [A] Wrap a Neuron's raw output in a neutral AGENT_OUTPUT envelope.
    The Cortex decides what this becomes (FINAL, MEMORY_APPEND, next TASK…).
    """
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
    """
    [A] The Neuron needs more information before it can complete the task.
    Emitted by the Axon when it detects a clarification signal in the agent's output.
    """
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
    """
    [C] Workflow concluded. result carries the terminal output.
    cost (optional) rolls up total token/compute cost for the trace.
    """
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
    """
    [A][C] Something went wrong.
    recoverable=True means the Cortex may retry or reroute.
    """
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
    """
    [A] Neuron connecting to the Synapse and declaring its capabilities.
    Emitted once by the Axon on startup.
    """
    payload: dict[str, Any] = {"capabilities": capabilities}
    if version:
        payload["version"] = version
    return Signal(
        type=SignalType.REGISTER,
        trace_id=new_trace_id(),  # management signals get their own trace
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
    """[A] Neuron disconnecting from the Synapse."""
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
    """[A] Periodic liveness signal from a Neuron."""
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
    """[C] Write a value to the shared Engram under the given key."""
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
    """
    [C] Broadcast a task to candidate Neurons for competitive bidding.
    Neurons reply with BID signals.
    """
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
    """[C] Cortex bids on behalf of a Neuron in response to a TASK_OFFER."""
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
    """
    [C] Peer review of another Neuron's output.
    target_event_id is the id of the AGENT_OUTPUT being critiqued.
    verdict: 'pass' | 'fail' | 'revise'
    """
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
