"""
cosmonapse.glia.base
~~~~~~~~~~~~~~~~~~~~
The Glia card: a thin two-way gate wrapped around one component.

An Axon, an Effector or an Engram may carry one card. The card reads
every signal going into its component and every signal coming out of it,
and enforces its policies on them. There is no orchestrator and no
central policy process: a card is a local object, consulted in-process,
never over the wire. The Dendrite has no policy role; it routes and
publishes, and only holds a card for trace-wide limits.

The pieces a caller needs:

  ``Glia``         the card. ``Glia.load(...)`` for the versioned
                   artifact, or the constructor for a hand-built one.
  ``Verdict``      what a policy returns: allow / deny / redact /
                   escalate, with a reason and (for redact) a
                   replacement payload.
  ``Direction``    inbound / outbound / both. Default both.
  ``Retry``        what a violation does next: reask / resend / none.
  ``SignalView``   the frozen, read-only view a policy handler receives.
  ``Mode``         off (default) / audit / enforce.

There are no checkpoints. A policy's scope is its ``direction`` and its
``types`` (signal types), and the default scope is every signal both
ways. What used to be ``on_tool_call`` is ``types=["TOOL_CALL"]``.

Handler semantics copy ``Effector.serve``'s settled contract exactly:
handlers run in registration order, the first non-None return answers,
None falls through. Sync or async.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from cosmonapse.envelope import Signal, SignalType

if TYPE_CHECKING:
    from cosmonapse.glia.artifact import PolicyArtifact
    from cosmonapse.glia.journal import Journal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# meta.glia field names
# ---------------------------------------------------------------------------
# Settled once, here, as constants  -  GLIA_DESIGN section 14. Everything
# that writes or reads the bus record goes through these names, so the
# spelling is changeable in one place and Genesis/Prism can read the
# vocabulary out of ``describe()`` rather than hand-mirroring it.

#: The ``meta`` key the whole record hangs off. ENVELOPE_SPEC section 2
#: makes ``meta`` explicitly ignorable, which is why a verdict rides here
#: instead of in a new SignalType.
META_KEY = "glia"

F_VERDICT = "verdict"
F_POLICY_ID = "policy_id"
F_POLICY_VERSION = "policy_version"
F_CARD_ID = "card_id"
F_DIRECTION = "direction"
F_SIGNAL = "signal"
F_RETRY = "retry"
F_COMPONENT = "component"
F_HASH = "hash"
F_MODE = "mode"
F_ATTEMPT = "attempt"
F_ATTEMPTS = "attempts"
F_LIMIT = "limit"
F_ISSUED_AT = "issued_at"
F_CARD_HASH = "card_hash"

#: Exactly the fields allowed to cross the Synapse. Enforced by
#: ``_bus_record``: policy id, policy version, direction, signal type,
#: verdict, the
#: component identity and a hash  -  and never the matched content, which
#: would leak what the policy was protecting (hard constraint 8).
BUS_FIELDS: frozenset[str] = frozenset({
    F_VERDICT, F_POLICY_ID, F_POLICY_VERSION, F_CARD_ID, F_DIRECTION,
    F_SIGNAL, F_RETRY, F_COMPONENT, F_HASH, F_MODE, F_ATTEMPT, F_ATTEMPTS,
    F_LIMIT,
})

#: What a REGISTER announces about the card the participant is running.
#: Distinct from BUS_FIELDS: this is a card announcement, not a verdict
#: record, and it carries no hash of anything a policy matched. Card age
#: is telemetry - there is no card lifetime, so the fleet view showing a
#: component on a six-week-old card is how revocation gets noticed
#: (GLIA_DESIGN section 11.1).
REGISTER_FIELDS: frozenset[str] = frozenset({
    F_CARD_ID, F_POLICY_VERSION, F_MODE, F_ISSUED_AT, F_CARD_HASH,
})

#: Prefix for a shadow verdict recorded in ``audit`` mode. A deny in audit
#: records ``would_deny`` on the signal it did not block.
WOULD_PREFIX = "would_"

#: Environment variable the deployment (pod image) sets to choose the mode.
#: The library default is always ``off``.
MODE_ENV_VAR = "COSMONAPSE_POLICY_MODE"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Mode(str, Enum):
    """One setting, three values, default ``off``.

    ``off``      no gate reads anything. Existing brains are unaffected.
    ``audit``    gates read and verdicts are recorded, nothing is
                 blocked. The migration path, and not optional: turning
                 ``enforce`` on without a shadow period is a guess.
    ``enforce``  verdicts are applied.
    """

    OFF = "off"
    AUDIT = "audit"
    ENFORCE = "enforce"

    @classmethod
    def coerce(cls, value: Mode | str | None) -> Mode:
        if value is None:
            return cls.OFF
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise PolicyError(
                f"unknown policy mode {value!r}; expected one of "
                f"{[m.value for m in cls]}"
            ) from None


class Direction(str, Enum):
    """Which way a signal is crossing the component a card is mounted on.

    A card is mounted on ONE component, so ``inbound`` always means
    "arriving at this component" and ``outbound`` means "leaving it". The
    same IMPRINT is outbound for the calling Axon's card and inbound for
    the Engram's card. ``both`` is a policy's scope, never a signal's.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"
    BOTH = "both"

    @classmethod
    def coerce(cls, value: Direction | str | None) -> Direction:
        if value is None:
            return cls.BOTH
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise PolicyError(
                f"unknown direction {value!r}; expected one of "
                f"{[d.value for d in cls]}"
            ) from None

    def covers(self, crossing: Direction) -> bool:
        """Whether a policy scoped to ``self`` applies to a signal
        crossing in ``crossing``."""
        return self is Direction.BOTH or self is crossing

    def crossings(self) -> tuple[Direction, ...]:
        if self is Direction.BOTH:
            return (Direction.INBOUND, Direction.OUTBOUND)
        return (self,)


class Retry(str, Enum):
    """What a violation (a ``deny``) does next. Set per policy.

    ``reask``   re-run the Axon's Neuron with the refusal as the correction
                input (the correction repair loop). An Axon only: an
                Effector or an Engram has no generator to re-ask.
    ``resend``  repeat the request that produced the violating reply,
                unchanged, and read the new reply. Only for replies whose
                request is safe to repeat: TOOL_RESULT and RECALLED.
    ``none``    refuse now. The default.
    """

    REASK = "reask"
    RESEND = "resend"
    NONE = "none"

    @classmethod
    def coerce(cls, value: Retry | str | None) -> Retry:
        if value is None:
            return cls.NONE
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise PolicyError(
                f"unknown retry {value!r}; expected one of "
                f"{[r.value for r in cls]}"
            ) from None


def coerce_types(value: Any) -> frozenset[SignalType] | None:
    """A policy's ``types`` scope. None means every signal type.

    Accepts None, ``"*"``, ``["*"]``, or a list of SignalType names (or
    members). An unknown name is a load-time error rather than a rule
    that silently never fires.
    """
    if value is None:
        return None
    if isinstance(value, (str, SignalType)):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise PolicyError(
            f"types must be a list of signal type names, got "
            f"{type(value).__name__}"
        )
    out: set[SignalType] = set()
    for item in value:
        if isinstance(item, SignalType):
            out.add(item)
            continue
        name = str(item).strip().upper()
        if name == "*":
            return None
        try:
            out.add(SignalType[name])
        except KeyError:
            raise PolicyError(
                f"unknown signal type {item!r} in types; expected SignalType "
                f"names such as TASK, TOOL_CALL, RECALLED, or '*'"
            ) from None
    if not out:
        raise PolicyError("types must not be empty; omit it or use ['*']")
    return frozenset(out)


class Decision(str, Enum):
    """What a policy decided. ``escalate`` is only offered where a
    PERMISSION has somewhere to go: see :data:`ESCALATABLE`."""

    ALLOW = "allow"
    DENY = "deny"
    REDACT = "redact"
    ESCALATE = "escalate"

    @classmethod
    def coerce(cls, value: Decision | str) -> Decision:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise PolicyError(
                f"unknown verdict {value!r}; expected one of "
                f"{[d.value for d in cls]}"
            ) from None


class AuditKind(str, Enum):
    """The audit category a policy or repair event belongs to.

    This is the discriminator on a AUDIT signal's payload, and it is what
    makes one stream sliceable into separate audits: point a security
    review at ``GUARD_RETRY`` and ``GUARDED``, an eval review at
    ``EVAL_RETRY``, a tool-contract review at ``TOOL_RETRY``, and a
    reliability review at the rest.

    Four are re-asks or resends and are named for it. ``GUARDED``,
    ``LIMIT_REFUSED`` and ``DEADLINE_ABANDONED`` are NOT: the signal was
    refused, rewritten or escalated with no retry, the action was
    declined, or the loop gave up, and filing any of them under a
    ``_RETRY`` name would tell an auditor that something was retried when
    nothing was.
    """

    #: A Glia policy was violated and the violation is being retried, by
    #: re-asking the Neuron or resending the request, or the retries ran
    #: out. The security audit.
    GUARD_RETRY = "GUARD_RETRY"
    #: A Glia policy refused, rewrote or escalated a signal, with no retry.
    #: The security audit.
    GUARDED = "GUARDED"
    #: An output nothing claimed, under an Axon's strict mode. The eval
    #: audit: the generator produced something the harness could not read.
    EVAL_RETRY = "EVAL_RETRY"
    #: Tool-call arguments that failed their declared schema. The tool
    #: contract audit.
    TOOL_RETRY = "TOOL_RETRY"
    #: An Effector or Engram backend fault retried with an identical
    #: request. The reliability audit.
    TRANSIENT_RETRY = "TRANSIENT_RETRY"
    #: A Dendrite declined an action because the trace was at its limit.
    #: Not a retry: nothing was re-asked.
    LIMIT_REFUSED = "LIMIT_REFUSED"
    #: A repair loop abandoned because its time budget ran out before the
    #: caller's timeout would. Not a retry: the next attempt never ran.
    DEADLINE_ABANDONED = "DEADLINE_ABANDONED"


#: Which audit each kind rolls up to. Carried in the AUDIT payload as
#: ``audit`` so a compliance consumer subscribed to the AUDIT subject and
#: nothing else needs no second lookup to route the record.
AUDIT_DOMAIN: dict[AuditKind, str] = {
    AuditKind.GUARD_RETRY: "security",
    AuditKind.GUARDED: "security",
    AuditKind.EVAL_RETRY: "eval",
    AuditKind.TOOL_RETRY: "tool",
    AuditKind.TRANSIENT_RETRY: "reliability",
    AuditKind.LIMIT_REFUSED: "governance",
    AuditKind.DEADLINE_ABANDONED: "reliability",
}


class AuditOutcome(str, Enum):
    """What happened to the event the AUDIT records."""

    #: The generator was asked again.
    RE_ASKED = "re_asked"
    #: The request was sent again, unchanged (a ``retry: resend`` policy).
    RESENT = "resent"
    #: An identical retry succeeded (transient only).
    RECOVERED = "recovered"
    #: Out of attempts; the refusal or observation went out as the answer.
    EXHAUSTED = "exhausted"
    #: The next attempt would have been identical, so it was not made.
    UNCORRECTABLE = "uncorrectable"
    #: Declined outright, with no re-ask available at this point.
    REFUSED = "refused"
    #: The subject was rewritten on a copy and the step continued.
    REDACTED = "redacted"
    #: A PERMISSION was raised instead of acting.
    ESCALATED = "escalated"
    #: ``audit`` mode: this WOULD have been blocked, and was not.
    WOULD_BLOCK = "would_block"


#: How a repair trigger maps onto its audit category.
KIND_FOR_TRIGGER: dict[str, AuditKind] = {
    "policy_refusal": AuditKind.GUARD_RETRY,
    "unclaimed_output": AuditKind.EVAL_RETRY,
    "invalid_tool_args": AuditKind.TOOL_RETRY,
    "transient": AuditKind.TRANSIENT_RETRY,
}


#: Which verdict produced a shaping, as a AUDIT outcome.
OUTCOME_FOR_DECISION: dict[str, AuditOutcome] = {
    "deny": AuditOutcome.REFUSED,
    "redact": AuditOutcome.REDACTED,
    "escalate": AuditOutcome.ESCALATED,
}


#: Where ``escalate`` has somewhere to go: an Axon can raise a PERMISSION
#: instead of running a TASK, instead of emitting its output, or instead of
#: sending a tool call. An Effector or an Engram has no escalation channel,
#: so a rule asking for one elsewhere is refused at load and a handler
#: asking for one elsewhere fails closed.
ESCALATABLE: frozenset[tuple[Direction, SignalType]] = frozenset({
    (Direction.INBOUND, SignalType.TASK),
    (Direction.OUTBOUND, SignalType.AGENT_OUTPUT),
    (Direction.OUTBOUND, SignalType.TOOL_CALL),
})

#: Replies whose request is safe to repeat, so ``retry: resend`` is
#: offered on them. IMPRINTED is deliberately absent: resending an IMPRINT
#: repeats a write, and not every backend's writes are idempotent.
RESENDABLE: frozenset[SignalType] = frozenset({
    SignalType.TOOL_RESULT, SignalType.RECALLED,
})

#: Replies an Axon receives to its own calls. A violation on one of these
#: can be re-asked: the Neuron is told the reply was refused and tries
#: again.
REASKABLE_INBOUND: frozenset[SignalType] = frozenset({
    SignalType.TOOL_RESULT, SignalType.RECALLED, SignalType.IMPRINTED,
})


def allowed_retry(
    retry: Retry, direction: Direction, signal_type: SignalType, *,
    can_reask: bool,
) -> Retry:
    """The retry a violation actually gets at this point.

    The card validates what it can at load, but whether a Neuron exists to
    re-ask depends on which component the card is mounted on. A retry that
    has nowhere to go degrades to ``none`` rather than looping on nothing.
    """
    if retry is Retry.RESEND:
        return retry if signal_type in RESENDABLE else Retry.NONE
    if retry is Retry.REASK:
        if not can_reask:
            return Retry.NONE
        if direction is Direction.INBOUND and signal_type not in REASKABLE_INBOUND:
            return Retry.NONE
    return retry

#: Default replacement substituted into a matched string by an artifact
#: redact rule. A decorator handler replaces the subject wholesale instead.
DEFAULT_REPLACEMENT = "[redacted]"

_UNSET: Any = object()


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """What a policy returns. Build one with the four constructors.

    ``reason`` is author-written static text. It is the one string a
    refusal puts where the model can read and correct from, so it must
    describe the rule, never quote what matched. ``matched`` is the
    opposite: it carries the matched content and goes to the component's
    local journal only, never onto the bus.
    """

    decision: Decision
    reason: str | None = None
    replacement: Any = None
    policy_id: str | None = None
    matched: str | None = None
    #: Set when redact actually changed something, so a no-op redaction is
    #: not recorded as a shaping event.
    substitutions: int = 0

    @classmethod
    def allow(cls, *, policy_id: str | None = None) -> Verdict:
        return cls(decision=Decision.ALLOW, policy_id=policy_id)

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        policy_id: str | None = None,
        matched: str | None = None,
    ) -> Verdict:
        return cls(
            decision=Decision.DENY, reason=reason, policy_id=policy_id,
            matched=matched,
        )

    @classmethod
    def redact(
        cls,
        replacement: Any,
        *,
        reason: str | None = None,
        policy_id: str | None = None,
        matched: str | None = None,
        substitutions: int = 1,
    ) -> Verdict:
        """Replace the subject with ``replacement``. The caller applies it
        to a COPY  -  mutating the subject in place would rewrite what
        every other in-process subscriber holds (hard constraint 7)."""
        return cls(
            decision=Decision.REDACT, reason=reason,
            replacement=replacement, policy_id=policy_id, matched=matched,
            substitutions=substitutions,
        )

    @classmethod
    def escalate(
        cls,
        reason: str,
        *,
        policy_id: str | None = None,
        matched: str | None = None,
    ) -> Verdict:
        return cls(
            decision=Decision.ESCALATE, reason=reason, policy_id=policy_id,
            matched=matched,
        )

    @property
    def is_allow(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def shapes(self) -> bool:
        """Whether this verdict changes what the component does."""
        return self.decision is not Decision.ALLOW


# ---------------------------------------------------------------------------
# Outcome and refusal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyOutcome:
    """One gate evaluation, after the mode has been applied.

    ``declared`` is what the policy said; ``effective`` is what the
    component must do about it, which in ``audit`` is always ``allow``.
    ``record`` is the thin bus record: attach it under ``meta[META_KEY]``
    on the signal the verdict shaped. ``retry`` and ``max_attempts`` come
    from the policy that decided, and only mean anything on a deny.
    """

    direction: Direction
    signal_type: SignalType
    declared: Verdict
    effective: Decision
    record: dict[str, Any]
    applied: bool
    retry: Retry = Retry.NONE
    max_attempts: int = 0

    @property
    def reason(self) -> str | None:
        return self.declared.reason

    @property
    def replacement(self) -> Any:
        return self.declared.replacement

    @property
    def denied(self) -> bool:
        return self.effective is Decision.DENY

    @property
    def redacted(self) -> bool:
        return self.effective is Decision.REDACT

    @property
    def escalated(self) -> bool:
        return self.effective is Decision.ESCALATE

    @property
    def shaped(self) -> bool:
        """Whether the component must act on this outcome at all."""
        return self.applied and self.effective is not Decision.ALLOW

    @property
    def scope(self) -> str:
        """``inbound:TOOL_RESULT``, for a refusal a model or a person reads."""
        return f"{self.direction.value}:{self.signal_type.value}"


class PolicyRefusal(Exception):
    """A gate denied or escalated a signal.

    Raised to the code that was sending or awaiting it: inside a Neuron
    for its own ``recall`` / ``imprint`` / ``call_tool``, or to the
    component's servicing path. The component then answers with the reply
    the step would have produced anyway, with ``error`` set and
    ``meta.glia`` attached. A policy verdict never produces an ERROR
    signal (hard constraint 3).

    ``retry`` says what the policy asked for next. A Neuron that catches
    this and carries on has handled it; one that lets it propagate hands it
    back to the Axon, which re-asks (``reask``) or answers with the
    refusal (``none``).
    """

    def __init__(self, outcome: PolicyOutcome, *, retry: Retry | None = None) -> None:
        self.outcome = outcome
        self.retry = outcome.retry if retry is None else retry
        #: Set once an AUDIT record for this refusal has gone out, so the
        #: Axon does not record the same event twice.
        self.audited = False
        super().__init__(outcome.reason or "refused by policy")

    @property
    def decision(self) -> Decision:
        return self.outcome.effective

    @property
    def reason(self) -> str:
        return self.outcome.reason or "refused by policy"

    @property
    def record(self) -> dict[str, Any]:
        return self.outcome.record


# ---------------------------------------------------------------------------
# What a policy handler sees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalView:
    """A frozen, read-only view of one signal crossing a gate.

    ``payload`` is a deep copy, so a handler can read it freely; to change
    it, return ``Verdict.redact(new_payload)``. ``meta`` is a read-only
    mapping over a deep copy. The envelope fields cannot be changed by a
    policy at all.

    ``directed_id`` IS A CLAIM, NOT AN IDENTITY. Until an envelope carries
    a signature (GLIA_DESIGN section 11), any participant on a shared
    broker can publish any ``directed.id``. A policy on it stops mistakes
    and misrouted traffic, not an attacker on the bus.
    """

    type: SignalType
    id: str
    trace_id: str
    parent_id: str | None
    directed_id: str | None
    directed_type: str | None
    directed_capabilities: tuple[str, ...]
    ts: datetime
    meta: Any
    payload: dict[str, Any]
    #: Which way this signal is crossing the gate.
    direction: Direction
    #: The component the gate is mounted on.
    component: str | None

    @classmethod
    def of(
        cls, signal: Signal, direction: Direction, component: str | None,
    ) -> SignalView:
        d = signal.directed
        return cls(
            type=signal.type,
            id=signal.id,
            trace_id=signal.trace_id,
            parent_id=signal.parent_id,
            directed_id=d.id if d else None,
            directed_type=d.type if d else None,
            directed_capabilities=tuple((d.capabilities or []) if d else ()),
            ts=signal.ts,
            meta=MappingProxyType(copy.deepcopy(dict(signal.meta))),
            payload=copy.deepcopy(dict(signal.payload)),
            direction=direction,
            component=component,
        )


def with_payload(signal: Signal, payload: dict[str, Any]) -> Signal:
    """A COPY of ``signal`` carrying ``payload``, same id and envelope.

    Never a mutation: ``MemorySynapse`` hands the same Signal object to
    every in-process subscriber (hard constraint 7).
    """
    copied: Signal = signal.model_copy(update={
        "payload": dict(payload), "meta": dict(signal.meta),
    })
    return copied


def with_glia_record(signal: Signal, record: dict[str, Any]) -> Signal:
    """A copy of ``signal`` with ``record`` merged under ``meta.glia``."""
    if not record:
        return signal
    meta = dict(signal.meta)
    meta[META_KEY] = {**(meta.get(META_KEY) or {}), **record}
    copied: Signal = signal.model_copy(update={"meta": meta})
    return copied


# Signals a gate or the repair loop produced itself: refusal replies, the
# PERMISSION an escalation raises, the loop-broken ERROR. They pass every
# gate unread, or a deny rule could deny its own refusal and the caller
# would hang. Keyed by signal id, which is globally unique, and bounded:
# a process that runs forever must not grow a row per refusal it ever
# made. AUDIT records are exempt by type, so a card cannot gate the
# stream that reports on it.
_EXEMPT: dict[str, None] = {}
_MAX_EXEMPT = 4096


def mark_exempt(signal: Signal) -> Signal:
    """Mark ``signal`` as gate-generated, so no gate reads it. Returns it."""
    if len(_EXEMPT) >= _MAX_EXEMPT:
        _EXEMPT.pop(next(iter(_EXEMPT)), None)
    _EXEMPT[signal.id] = None
    return signal


def is_exempt(signal: Signal) -> bool:
    return signal.type is SignalType.AUDIT or signal.id in _EXEMPT


class PolicyError(ValueError):
    """A card is malformed, or asks for something its scope cannot
    offer. Raised at load/construction time, never at request time.

    A ``ValueError`` subclass so the constructors keep raising what a
    caller passing a bad value already expects, while a card loader can
    catch exactly this.
    """


# ---------------------------------------------------------------------------
# Card sub-settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairPolicy:
    """How far the correction repair loop may go inside one component.

    ``max_attempts`` counts corrections, not the first pass, and is what
    contains the loop: past it the component stops offering a correctable
    refusal and emits a real ERROR with ``recoverable=False``  -  the one
    legitimate ERROR in this design, because the loop is broken rather
    than the request (GLIA_DESIGN section 8.4).

    ``emit_audit`` publishes one AUDIT signal per policy or repair event,
    AT the event rather than at the next attempt's start. On by default
    once a card is in ``audit`` or ``enforce``, because that is what makes
    the absence of a AUDIT on a trace mean "nothing fired" rather than
    "nothing was recorded". Turn it off on a high-traffic card whose
    redactions would double its signal volume; the verdict still rides
    the reply's ``meta.glia`` either way.

    ``deadline_s`` bounds the whole loop. Unset means unbounded, which is
    the honest default since the Axon cannot see the caller's
    ``timeout_s``. Set it below that timeout and the loop abandons itself
    with a DEADLINE_ABANDONED record instead of letting the outer
    RetryStrategy time out mid-repair, re-dispatch on a fresh trace and
    STOP an attempt that was about to succeed.
    """

    max_attempts: int = 2
    emit_audit: bool = True
    deadline_s: float | None = None
    #: Cap on the ``meta.glia.attempts`` list carried on the wire.
    max_recorded: int = 8
    #: Transient (Effector / Engram) retries. A policy refusal NEVER feeds
    #: this: retrying an identical request against a deterministic policy
    #: is N guaranteed denials (GLIA_DESIGN section 8.1).
    transient_attempts: int = 0
    transient_backoff_s: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 0:
            raise PolicyError("repair.max_attempts must be >= 0")
        if self.transient_attempts < 0:
            raise PolicyError("repair.transient_attempts must be >= 0")
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise PolicyError("repair.deadline_s must be > 0 when set")


@dataclass(frozen=True)
class TraceLimits:
    """Trace-wide caps, enforced by the Dendrite rather than by a card.

    A card sees only its own component; trace totals belong on the
    Dendrite, which already receives the whole trace's traffic.

    ``component``  exact, and the default. Counts only what this Dendrite
                   emitted on the trace, so it needs no subscription. It
                   catches the common runaway, which is one component
                   spinning.
    ``trace``      approximate. Counts everything seen, so it needs the
                   broadcast subscription, and is soft under concurrency
                   by at most one action per concurrent actor.

    There is deliberately no mode named ``exact``: the count is only
    exact when nothing is concurrent. ``trace_strict`` is not built.
    """

    MODES: ClassVar[frozenset[str]] = frozenset({"component", "trace"})

    mode: str = "component"
    #: Cap on every counted signal this Dendrite emitted on the trace.
    max_actions: int | None = None
    max_tool_calls: int | None = None
    max_imprints: int | None = None
    #: ``refuse`` declines locally; ``stop_trace`` additionally asks the
    #: whole workflow to stop (cooperative cancellation with saga
    #: rollback, which stop_trace already does).
    on_exceed: str = "refuse"

    def __post_init__(self) -> None:
        if self.mode not in self.MODES:
            raise PolicyError(
                f"unknown trace-limit mode {self.mode!r}; expected one of "
                f"{sorted(self.MODES)}. trace_strict is not built  -  it "
                f"needs one authority for the count, so a round trip per "
                f"decision."
            )
        if self.on_exceed not in ("refuse", "stop_trace"):
            raise PolicyError(
                f"unknown on_exceed {self.on_exceed!r}; expected 'refuse' "
                f"or 'stop_trace'"
            )

    @property
    def active(self) -> bool:
        return any((
            self.max_actions is not None,
            self.max_tool_calls is not None,
            self.max_imprints is not None,
        ))


# ---------------------------------------------------------------------------
# Redaction: always onto a copy
# ---------------------------------------------------------------------------


def redact_text_leaves(
    obj: Any,
    patterns: tuple[re.Pattern[str], ...],
    replacement: str,
    drop_keys: frozenset[str] = frozenset(),
) -> tuple[Any, int]:
    """Return a COPY of ``obj`` with every pattern substituted in every
    string leaf, plus the number of substitutions made.

    Mapping keys named in ``drop_keys`` are removed from the copy. This
    never mutates ``obj``: ``MemorySynapse`` passes the ``Signal`` object
    by reference and never serialises, so rewriting in place would
    rewrite what every other in-process subscriber holds.
    """
    subs = 0
    if isinstance(obj, str):
        out = obj
        for pat in patterns:
            out, n = pat.subn(replacement, out)
            subs += n
        return out, subs
    if isinstance(obj, dict):
        new: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k in drop_keys:
                subs += 1
                continue
            nv, n = redact_text_leaves(v, patterns, replacement, drop_keys)
            subs += n
            new[k] = nv
        return new, subs
    if isinstance(obj, list):
        items = [
            redact_text_leaves(v, patterns, replacement, drop_keys)
            for v in obj
        ]
        return [v for v, _ in items], subs + sum(n for _, n in items)
    if isinstance(obj, tuple):
        titems = [
            redact_text_leaves(v, patterns, replacement, drop_keys)
            for v in obj
        ]
        return tuple(v for v, _ in titems), subs + sum(n for _, n in titems)
    return obj, subs


async def publish_audit(
    dendrite: Any,
    *,
    kind: AuditKind,
    outcome: AuditOutcome,
    trace_id: str,
    parent_id: str,
    component: str | None = None,
    attempt: int = 1,
    card: Glia | None = None,
    direction: Direction | None = None,
    signal_type: SignalType | None = None,
    policy_id: str | None = None,
    reason: str | None = None,
    digest: str | None = None,
    took_ms: int | None = None,
) -> None:
    """Publish one AUDIT record, best effort.

    The single funnel every emitter goes through, so the payload shape and
    the audit mapping are settled in one place. Silent when there is no
    hosting Dendrite (an unattached Axon in a unit test) or when the card
    turned the stream off.

    Failure here is swallowed: an audit record that cannot be published
    must not take down the step it was recording. The verdict is on the
    reply regardless.
    """
    if dendrite is None:
        return
    if card is not None and not card.repair.emit_audit:
        return
    try:
        await dendrite.emit_audit(
            trace_id=trace_id,
            parent_id=parent_id,
            kind=kind.value,
            domain=AUDIT_DOMAIN[kind],
            outcome=outcome.value,
            attempt=attempt,
            component=component,
            direction=None if direction is None else direction.value,
            signal=None if signal_type is None else signal_type.value,
            policy_id=policy_id,
            policy_version=None if card is None else card.version,
            card_id=None if card is None else card.card_id,
            reason=reason,
            hash=digest,
            took_ms=took_ms,
            neuron=component,
        )
    except Exception:
        logger.warning(
            "policy: AUDIT publish failed (kind=%s, trace=%s)",
            kind.value, trace_id, exc_info=True,
        )


async def audit_outcome(
    dendrite: Any,
    oc: PolicyOutcome,
    *,
    kind: AuditKind,
    outcome: AuditOutcome,
    trace_id: str,
    parent_id: str,
    component: str | None,
    card: Glia | None,
    attempt: int = 1,
) -> None:
    """``publish_audit`` for one gate event, with the scope, policy id,
    reason and digest taken off the outcome."""
    await publish_audit(
        dendrite,
        kind=kind,
        outcome=outcome,
        trace_id=trace_id,
        parent_id=parent_id,
        component=component,
        attempt=attempt,
        card=card,
        direction=oc.direction,
        signal_type=oc.signal_type,
        policy_id=oc.declared.policy_id,
        reason=oc.reason,
        digest=oc.record.get(F_HASH),
    )


def outcome_for(outcome_word: str, *, applied: bool) -> AuditOutcome:
    """The AUDIT outcome for a verdict that shaped something.

    In ``audit`` the verdict was recorded and nothing was blocked, so the
    record says ``would_block`` rather than claiming an action that did
    not happen.
    """
    if not applied:
        return AuditOutcome.WOULD_BLOCK
    return OUTCOME_FOR_DECISION.get(outcome_word, AuditOutcome.REFUSED)


async def run_with_transient_retry(
    call: Callable[[], Awaitable[Any]],
    policy: RepairPolicy,
    *,
    label: str,
) -> tuple[Any, list[dict[str, Any]]]:
    """Retry an IDENTICAL call while the world might still change.

    The Effector and Engram flavour of repair. There is nobody to
    re-ask on this side, so the input does not change between attempts
    and the hope is that a dropped connection, a 429 or lock contention
    has cleared. Bounded by attempts and backoff.

    A POLICY REFUSAL NEVER ARRIVES HERE, and the two flavours must not
    share a knob: retrying an identical request against a deterministic
    policy is N guaranteed denials. The guarded wrappers consult the
    card OUTSIDE this call and raise ``PolicyRefusal``, so a refusal can
    never be mistaken for a transient fault (GLIA_DESIGN section 8.1).

    Returns the value and the attempt records, capped for the wire. The
    records carry the exception TYPE, not its message: a backend's error
    text is not something to put on every reply.
    """
    attempts: list[dict[str, Any]] = []
    last: Exception | None = None
    for i in range(policy.transient_attempts + 1):
        started = asyncio.get_running_loop().time()
        try:
            value = await call()
        except asyncio.CancelledError:
            # A STOP cancelling the trace is not a transient fault.
            raise
        except Exception as exc:
            last = exc
            attempts.append({
                "attempt": i + 1,
                "trigger": "transient",
                "outcome": "retried",
                "took_ms": int(
                    (asyncio.get_running_loop().time() - started) * 1000
                ),
                "error": type(exc).__name__,
            })
            if i >= policy.transient_attempts:
                attempts[-1]["outcome"] = "exhausted"
                logger.warning(
                    "%s: transient retry exhausted after %d attempt(s)",
                    label, i + 1,
                )
                raise
            await asyncio.sleep(policy.transient_backoff_s * (i + 1))
            continue
        else:
            if attempts:
                attempts[-1]["outcome"] = "recovered"
            return value, attempts[-policy.max_recorded:]
    raise last if last is not None else RuntimeError(f"{label}: no attempt ran")


def canonical_text(obj: Any) -> str:
    """A stable text rendering of a subject, for matching and hashing.

    Deliberately NOT a signing canonicaliser  -  that is Phase 5 and has
    to be derived from the model with sorted keys and a defined datetime
    representation. This one only has to be deterministic enough that the
    same subject matches the same rules and hashes the same way.
    """
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(obj)


def content_hash(text: str) -> str:
    """Short digest of matched content, so a bus record can be correlated
    with the local journal entry without the content crossing."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

#: Context kwargs a policy handler may request by declaring the parameter.
#: Same idiom as ``_TOOL_HANDLER_KWARGS`` on a served Effector.
_HANDLER_KWARGS: frozenset[str] = frozenset({
    "trace_id", "component", "direction", "card",
})


def _wanted(fn: Callable[..., Any]) -> frozenset[str]:
    wants: set[str] = set()
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return frozenset()
    for pname, p in sig.parameters.items():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return _HANDLER_KWARGS
        if pname in _HANDLER_KWARGS:
            wants.add(pname)
    return frozenset(wants)


@dataclass(frozen=True)
class _Handler:
    """One decorator-registered policy and its scope."""

    fn: Callable[..., Any]
    wants: frozenset[str]
    direction: Direction
    types: frozenset[SignalType] | None
    retry: Retry
    max_attempts: int

    def selects(self, crossing: Direction, signal_type: SignalType) -> bool:
        return self.direction.covers(crossing) and (
            self.types is None or signal_type in self.types
        )


class Glia:
    """One card, one gate around one component.

    Mounted singular, never as a list::

        CARD = Glia.load("corp.card")

        Axon(..., glia=CARD)
        Effector.serve(..., glia=CARD)
        Engram.serve(..., glia=CARD)

    The card reads every signal entering and leaving the component it is
    mounted on. Each policy has a scope, ``direction`` (inbound /
    outbound / both, default both) and ``types`` (default every signal
    type), a verdict, and a ``retry`` for what a violation does next.

    A card is primarily a versioned artifact, because decorators alone do
    not survive a fleet: with twenty components nobody can answer "what
    is enforced right now" by reading twenty modules. Bespoke policies
    are declared ON THE CARD, not on the component, so they travel with
    it::

        @CARD.on_signal(direction="outbound", types=["TOOL_CALL"])
        def no_writes_after_hours(sig):
            if sig.payload.get("tool") == "write" and _after_hours():
                return Verdict.deny("writes are closed outside business hours")
            return None

    Both are evaluated, the artifact first.

    ``Dendrite(glia=...)`` still exists, for trace-wide limits only: a
    limit counts a trace, not a component, so it does not fit a gate. A
    Dendrite never evaluates a card's policies.

    An empty card does nothing. Cosmonapse ships the seam, not a PII
    regex library or an injection detector, so nobody should expect
    out-of-the-box protection from mounting one.
    """

    def __init__(
        self,
        *,
        card_id: str = "card",
        issued_to: str | None = None,
        version: str | None = None,
        issuer: str | None = None,
        issued_at: datetime | str | None = None,
        not_after: datetime | str | None = None,
        mode: Mode | str | None = None,
        fail_open: bool = False,
        artifact: PolicyArtifact | None = None,
        journal: Journal | None = None,
        repair: RepairPolicy | None = None,
        limits: TraceLimits | None = None,
        sample_pass: int = 0,
        card_hash: str | None = None,
    ) -> None:
        self.card_id = card_id
        #: The component identity this card was issued to. Advisory until
        #: Phase 5 makes identity the fingerprint of the card's public key.
        self.issued_to = issued_to
        #: The policy artifact version, reported on REGISTER so an audit
        #: record can be attributed to a policy version.
        self.version = version
        self.issuer = issuer
        self.issued_at = _as_dt(issued_at) or datetime.now(UTC)
        #: Present in the format, normally unset, NEVER enforced. There is
        #: deliberately no card lifetime (GLIA_DESIGN section 11.1);
        #: keeping the field means adding expiry later is a policy choice
        #: rather than a format change.
        self.not_after = _as_dt(not_after)
        #: Fail-closed is the default: a policy handler that itself raises
        #: denies in ``enforce``. Exposed per card, not per policy.
        self.fail_open = bool(fail_open)
        self.repair = repair or RepairPolicy()
        self.limits = limits or TraceLimits()
        self.card_hash = card_hash
        #: Record one pass verdict in N. 0 records none: auditing every
        #: allow in full inflates ``meta`` on every signal in the system.
        self.sample_pass = max(0, int(sample_pass))

        self._artifact = artifact
        self._journal = journal
        self._handlers: list[_Handler] = []
        # (direction, type) pairs anything is declared for: the null check
        # every gate sits behind, so a signal no policy selects costs one
        # set lookup.
        self._declared: set[tuple[Direction, SignalType]] = set()
        self._reindex()
        self._pass_seen = 0
        # Per-card counters, readable for telemetry. Not shared state: a
        # card belongs to one component in one process.
        self.counters: dict[str, int] = {
            "checked": 0, "denied": 0, "redacted": 0, "escalated": 0,
            "would_block": 0, "handler_failed": 0,
        }

        # The mode is the deployment's call. The library default is always
        # ``off`` so every existing brain keeps working with no card; the
        # pod image sets ``enforce`` through the artifact or the env var.
        env_mode = os.environ.get(MODE_ENV_VAR)
        chosen = env_mode or mode
        self.mode = Mode.coerce(chosen)

    # -- loading -------------------------------------------------------

    @classmethod
    def load(cls, source: str | bytes | os.PathLike[str]) -> Glia:
        """Load a card from a file path, raw bytes, or an env var name.

        A ``str`` that names an existing file is read as one; a ``str``
        that names a set environment variable is read from it; otherwise
        it is treated as the artifact text itself. Bytes are always the
        artifact.
        """
        from cosmonapse.glia.artifact import PolicyArtifact

        artifact = PolicyArtifact.load(source)
        return artifact.to_card()

    @classmethod
    def from_artifact(cls, artifact: PolicyArtifact) -> Glia:
        return artifact.to_card()

    @property
    def artifact(self) -> PolicyArtifact | None:
        return self._artifact

    @property
    def journal(self) -> Journal | None:
        return self._journal

    @property
    def active(self) -> bool:
        """Whether the gate reads anything. ``off`` means it does not."""
        return self.mode is not Mode.OFF

    @property
    def has_policies(self) -> bool:
        return bool(self._declared)

    # -- the decorator ---------------------------------------------------

    def on_signal(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        direction: Direction | str | None = None,
        types: Any = None,
        retry: Retry | str | None = None,
        max_attempts: int = 2,
    ) -> Any:
        """Register a policy handler: ``fn(sig: SignalView)``.

        Usable bare (``@CARD.on_signal``: every signal, both ways) or
        with a scope (``@CARD.on_signal(direction="inbound",
        types=["RECALLED"], retry="resend")``). The handler returns a
        :class:`Verdict` or None; None falls through to the next handler.
        It may declare ``trace_id`` / ``component`` / ``direction`` /
        ``card`` as keyword parameters to receive them (a ``**kwargs``
        catch-all receives all). Sync or async.
        """
        scope_dir = Direction.coerce(direction)
        scope_types = coerce_types(types)
        scope_retry = Retry.coerce(retry)
        _validate_scope(
            f"handler {getattr(fn, '__name__', '<handler>')}",
            scope_dir, scope_types, None, scope_retry, int(max_attempts),
        )

        def deco(f: Callable[..., Any]) -> Callable[..., Any]:
            self._handlers.append(_Handler(
                fn=f, wants=_wanted(f), direction=scope_dir,
                types=scope_types, retry=scope_retry,
                max_attempts=int(max_attempts),
            ))
            self._reindex()
            return f

        return deco(fn) if callable(fn) else deco

    def _reindex(self) -> None:
        declared: set[tuple[Direction, SignalType]] = set()
        for h in self._handlers:
            for d in h.direction.crossings():
                for st in (h.types or SignalType):
                    declared.add((d, st))
        if self._artifact is not None:
            declared |= self._artifact.declared()
        self._declared = declared

    @property
    def handlers(self) -> int:
        """How many decorator handlers are registered."""
        return len(self._handlers)

    def declares(self, direction: Direction, signal_type: SignalType) -> bool:
        """Whether any policy, artifact rule or handler, selects signals
        of ``signal_type`` crossing in ``direction``. The null check every
        gate sits behind."""
        return (direction, signal_type) in self._declared

    # -- evaluation ----------------------------------------------------

    async def check(
        self,
        signal: Signal,
        *,
        direction: Direction,
        component: str | None = None,
        trace_id: str | None = None,
    ) -> PolicyOutcome | None:
        """Evaluate ``signal`` crossing this card's component.

        Returns None when there is nothing to do (mode off, a signal no
        policy selects, or a gate-generated signal), so a caller's fast
        path is a single null check. The subject every matcher reads is
        the payload; a redact verdict's replacement is the new payload.
        """
        if self.mode is Mode.OFF or is_exempt(signal):
            return None
        if not self.declares(direction, signal.type):
            return None

        self.counters["checked"] += 1
        failed = False
        declared: Verdict | None = None
        retry = Retry.NONE
        max_attempts = 0
        subject = dict(signal.payload)
        trace = trace_id or signal.trace_id

        # The artifact first, then the decorators: decorators cover what
        # the artifact cannot express, not the other way round.
        if self._artifact is not None:
            try:
                hit = self._artifact.evaluate(
                    direction, signal.type, subject, component=component,
                )
            except Exception:
                logger.exception(
                    "Glia %s: artifact evaluation raised on %s %s",
                    self.card_id, direction.value, signal.type.value,
                )
                failed = True
            else:
                if hit is not None:
                    declared, retry, max_attempts = hit

        if declared is None and not failed:
            view = SignalView.of(signal, direction, component)
            found, failed = await self._run_handlers(
                view, component=component, trace_id=trace,
            )
            if found is not None:
                declared, retry, max_attempts = found

        if failed:
            self.counters["handler_failed"] += 1
            # Fail closed, with an explicit per-card opt-out. Either way
            # the failure is recorded.
            declared = (
                Verdict.allow(policy_id="policy_error")
                if self.fail_open
                else Verdict.deny(
                    "refused: a policy check failed to evaluate",
                    policy_id="policy_error",
                )
            )
            retry, max_attempts = Retry.NONE, 0

        if declared is None:
            declared = Verdict.allow()

        declared = self._sanitise(direction, signal.type, declared)
        return self._finish(
            direction, signal.type, declared, subject,
            component=component, trace_id=trace, failed=failed,
            retry=retry, max_attempts=max_attempts,
        )

    async def _run_handlers(
        self,
        view: SignalView,
        *,
        component: str | None,
        trace_id: str | None,
    ) -> tuple[tuple[Verdict, Retry, int] | None, bool]:
        for h in self._handlers:
            if not h.selects(view.direction, view.type):
                continue
            kwargs: dict[str, Any] = {}
            if "trace_id" in h.wants:
                kwargs["trace_id"] = trace_id
            if "component" in h.wants:
                kwargs["component"] = component
            if "direction" in h.wants:
                kwargs["direction"] = view.direction
            if "card" in h.wants:
                kwargs["card"] = self
            try:
                result = h.fn(view, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                logger.exception(
                    "Glia %s: %s handler raised on %s %s",
                    self.card_id, getattr(h.fn, "__name__", "<handler>"),
                    view.direction.value, view.type.value,
                )
                return None, True
            if result is None:
                continue
            if not isinstance(result, Verdict):
                logger.error(
                    "Glia %s: %s handler returned %r; a handler returns a "
                    "Verdict or None",
                    self.card_id, getattr(h.fn, "__name__", "<handler>"),
                    type(result).__name__,
                )
                return None, True
            if result.decision is Decision.REDACT and not isinstance(
                result.replacement, dict,
            ):
                logger.error(
                    "Glia %s: %s handler redacted with a %s; a redact "
                    "replacement is the new payload, a dict",
                    self.card_id, getattr(h.fn, "__name__", "<handler>"),
                    type(result.replacement).__name__,
                )
                return None, True
            return (result, h.retry, h.max_attempts), False
        return None, False

    def _sanitise(
        self, direction: Direction, signal_type: SignalType, verdict: Verdict,
    ) -> Verdict:
        """An ``escalate`` with nowhere to go is the policy error it is:
        fail closed rather than let it silently allow the effect."""
        if (
            verdict.decision is Decision.ESCALATE
            and (direction, signal_type) not in ESCALATABLE
        ):
            logger.error(
                "Glia %s: escalate is not offered on %s %s; failing closed",
                self.card_id, direction.value, signal_type.value,
            )
            return Verdict.deny(
                verdict.reason or "refused by policy",
                policy_id=verdict.policy_id, matched=verdict.matched,
            )
        return verdict

    def _finish(
        self,
        direction: Direction,
        signal_type: SignalType,
        declared: Verdict,
        subject: Any,
        *,
        component: str | None,
        trace_id: str | None,
        failed: bool,
        retry: Retry,
        max_attempts: int,
    ) -> PolicyOutcome:
        audit_only = self.mode is Mode.AUDIT

        if declared.is_allow:
            effective = Decision.ALLOW
            applied = False
        elif audit_only:
            # The gate reads and records; nothing is blocked or retried.
            effective = Decision.ALLOW
            applied = False
            self.counters["would_block"] += 1
        else:
            effective = declared.decision
            applied = True
            self.counters[_COUNTER_FOR[effective]] += 1

        record = self._bus_record(
            direction, signal_type, declared,
            component=component, audit_only=audit_only, subject=subject,
            retry=retry,
        )
        self._journal_record(
            direction, signal_type, declared, subject,
            component=component, trace_id=trace_id,
            effective=effective, applied=applied, failed=failed,
        )
        return PolicyOutcome(
            direction=direction, signal_type=signal_type, declared=declared,
            effective=effective, record=record, applied=applied,
            retry=retry, max_attempts=max_attempts,
        )

    def _bus_record(
        self,
        direction: Direction,
        signal_type: SignalType,
        declared: Verdict,
        *,
        component: str | None,
        audit_only: bool,
        subject: Any,
        retry: Retry,
    ) -> dict[str, Any]:
        """The thin record, for ``meta.glia`` on the signal the verdict
        shaped.

        Policy id, policy version, scope, verdict, the component identity
        and a hash, and nothing else. The matched text goes to the local
        journal only; otherwise the audit record leaks exactly what the
        policy was protecting.
        """
        if declared.is_allow:
            # Pass records stay minimal, and are sampled: auditing every
            # allow in full inflates ``meta`` on every signal.
            self._pass_seen += 1
            if self.sample_pass <= 0 or self._pass_seen % self.sample_pass:
                return {}
            rec: dict[str, Any] = {
                F_VERDICT: Decision.ALLOW.value,
                F_DIRECTION: direction.value,
                F_SIGNAL: signal_type.value,
                F_MODE: self.mode.value,
            }
            if self.version:
                rec[F_POLICY_VERSION] = self.version
            if declared.policy_id:
                rec[F_POLICY_ID] = declared.policy_id
            return rec

        verdict_word = declared.decision.value
        if audit_only:
            verdict_word = f"{WOULD_PREFIX}{verdict_word}"
        rec = {
            F_VERDICT: verdict_word,
            F_DIRECTION: direction.value,
            F_SIGNAL: signal_type.value,
            F_CARD_ID: self.card_id,
            F_MODE: self.mode.value,
            F_HASH: content_hash(declared.matched or canonical_text(subject)),
        }
        if declared.decision is Decision.DENY and retry is not Retry.NONE:
            rec[F_RETRY] = retry.value
        if declared.policy_id:
            rec[F_POLICY_ID] = declared.policy_id
        if self.version:
            rec[F_POLICY_VERSION] = self.version
        if component:
            rec[F_COMPONENT] = component
        leaked = set(rec) - BUS_FIELDS
        if leaked:  # pragma: no cover - guards a future edit, not a path
            raise PolicyError(
                f"bus record carries non-bus fields {sorted(leaked)}; only "
                f"{sorted(BUS_FIELDS)} may cross the Synapse"
            )
        return rec

    def _journal_record(
        self,
        direction: Direction,
        signal_type: SignalType,
        declared: Verdict,
        subject: Any,
        *,
        component: str | None,
        trace_id: str | None,
        effective: Decision,
        applied: bool,
        failed: bool,
    ) -> None:
        """The full record, local and append-only. This is the only place
        matched content is written, and it never crosses the Synapse."""
        if self._journal is None:
            return
        if declared.is_allow and not failed and self.sample_pass <= 0:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "card_id": self.card_id,
            "policy_version": self.version,
            "policy_id": declared.policy_id,
            "direction": direction.value,
            "signal": signal_type.value,
            "declared": declared.decision.value,
            "effective": effective.value,
            "applied": applied,
            "mode": self.mode.value,
            "component": component,
            "trace_id": trace_id,
            "reason": declared.reason,
            "hash": content_hash(declared.matched or canonical_text(subject)),
            "matched": declared.matched,
            "policy_error": failed,
        }
        try:
            self._journal.record(entry)
        except Exception:
            # Best effort. A journal that cannot be written must not take
            # the component down with it.
            logger.exception(
                "Glia %s: journal write failed", self.card_id,
            )

    # -- introspection -------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """This card, as data. Genesis renders a card editor from this
        rather than hand-mirroring the fields."""
        return {
            "card_id": self.card_id,
            "issued_to": self.issued_to,
            "issuer": self.issuer,
            "issued_at": self.issued_at.isoformat(),
            "not_after": self.not_after.isoformat() if self.not_after else None,
            "policy_version": self.version,
            "card_hash": self.card_hash,
            "mode": self.mode.value,
            "fail_open": self.fail_open,
            "sample_pass": self.sample_pass,
            "repair": {
                "max_attempts": self.repair.max_attempts,
                "emit_audit": self.repair.emit_audit,
                "deadline_s": self.repair.deadline_s,
                "max_recorded": self.repair.max_recorded,
                "transient_attempts": self.repair.transient_attempts,
                "transient_backoff_s": self.repair.transient_backoff_s,
            },
            "limits": {
                "mode": self.limits.mode,
                "max_actions": self.limits.max_actions,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_imprints": self.limits.max_imprints,
                "on_exceed": self.limits.on_exceed,
            },
            "journal": None if self._journal is None else self._journal.describe(),
            "handlers": [
                {
                    "name": getattr(h.fn, "__name__", "<handler>"),
                    "direction": h.direction.value,
                    "types": (
                        None if h.types is None
                        else sorted(t.value for t in h.types)
                    ),
                    "retry": h.retry.value,
                    "max_attempts": h.max_attempts,
                }
                for h in self._handlers
            ],
            "policies": (
                [] if self._artifact is None else self._artifact.describe()
            ),
            "counters": dict(self.counters),
        }

    def __repr__(self) -> str:
        return (
            f"Glia(card_id={self.card_id!r}, mode={self.mode.value!r}, "
            f"version={self.version!r})"
        )


def _validate_scope(
    label: str,
    direction: Direction,
    types: frozenset[SignalType] | None,
    decision: Decision | None,
    retry: Retry,
    max_attempts: int,
) -> None:
    """Refuse at load what can never work, with the reason.

    Shared by artifact rules and decorator handlers, so a card editor and
    a hand-written card fail the same way.
    """
    if max_attempts < 0:
        raise PolicyError(f"{label}: max_attempts must be >= 0")
    if retry is Retry.RESEND and (types is None or not types <= RESENDABLE):
        raise PolicyError(
            f"{label}: retry=resend repeats the request behind a reply, "
            f"so types must name only replies safe to repeat: "
            f"{sorted(t.value for t in RESENDABLE)}. Resending an "
            f"outbound request would repeat a signal the same policy "
            f"just denied."
        )
    if retry is Retry.REASK and types is not None and (
        direction is Direction.INBOUND and types == {SignalType.TASK}
    ):
        raise PolicyError(
            f"{label}: retry=reask has no Neuron to re-ask about an inbound "
            f"TASK; the input arrived from outside and no attempt of ours "
            f"changes it"
        )
    if decision is Decision.ESCALATE:
        crossings = direction.crossings()
        if types is None or any(
            (d, t) not in ESCALATABLE for d in crossings for t in types
        ):
            raise PolicyError(
                f"{label}: escalate is only offered where a PERMISSION has "
                f"somewhere to go: "
                f"{sorted(f'{d.value}:{t.value}' for d, t in ESCALATABLE)}. "
                f"Name the direction and the types explicitly."
            )


def register_meta(card: Glia | None) -> dict[str, Any] | None:
    """What to put under ``meta.glia`` on a REGISTER, or None.

    Announcing the card and its policy version is what lets an audit
    record be attributed to a policy version, and what lets a fleet view
    report card age. It says nothing about any individual verdict.
    """
    if card is None:
        return None
    rec: dict[str, Any] = {
        F_CARD_ID: card.card_id,
        F_MODE: card.mode.value,
        F_ISSUED_AT: card.issued_at.isoformat(),
    }
    if card.version:
        rec[F_POLICY_VERSION] = card.version
    if card.card_hash:
        rec[F_CARD_HASH] = card.card_hash
    return rec


_COUNTER_FOR: dict[Decision, str] = {
    Decision.DENY: "denied",
    Decision.REDACT: "redacted",
    Decision.ESCALATE: "escalated",
}


def _as_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise PolicyError(f"not an ISO-8601 timestamp: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Trace-wide counting  -  owned by the Dendrite, not by a card
# ---------------------------------------------------------------------------


@dataclass
class _TraceTally:
    actions: int = 0
    tool_calls: int = 0
    imprints: int = 0
    refused: bool = False


#: Bound on how many traces one counter remembers. A Dendrite that runs
#: forever must not grow a row per trace it ever saw; the oldest are
#: dropped, which loses a count rather than leaking memory.
_MAX_TRACES = 4096

#: Bound on how many signal ids one counter remembers for deduplication.
#: Ids are ULIDs and globally unique, so one flat set serves every trace.
#: Past the bound the oldest are dropped, which can let a very old signal
#: be counted twice. That is the right way round: a bounded set that
#: occasionally over-counts beats an unbounded one that leaks.
_MAX_SEEN = 8192


class TraceCounter:
    """Per-trace action counts for one Dendrite.

    ``component`` mode is exact and needs no subscription: it counts only
    what this Dendrite emitted on the trace, which catches the common
    runaway  -  one component spinning. ``trace`` mode counts everything
    seen, which needs the broadcast subscription the Dendrite already has
    for Pathways, and is approximate under concurrency by at most one
    action per concurrent actor.
    """

    def __init__(self, limits: TraceLimits) -> None:
        self.limits = limits
        self._tallies: dict[str, _TraceTally] = {}
        # An ordered set, for deduplicating a signal counted on both the
        # outbound and the inbound side. A dict because it preserves
        # insertion order, which makes eviction FIFO.
        self._seen: dict[str, None] = {}

    def _tally(self, trace_id: str) -> _TraceTally:
        t = self._tallies.get(trace_id)
        if t is None:
            if len(self._tallies) >= _MAX_TRACES:
                self._tallies.pop(next(iter(self._tallies)), None)
            t = _TraceTally()
            self._tallies[trace_id] = t
        return t

    def count(
        self,
        trace_id: str,
        kind: str = "action",
        *,
        signal_id: str | None = None,
    ) -> None:
        """Record one action on ``trace_id``.

        ``kind`` is ``action``, ``tool_call`` or ``imprint`` (the latter
        two also count as an action), or ``none`` for traffic that is
        not a workflow action at all.

        ``signal_id`` makes the count idempotent per signal. Both modes
        count on the way OUT; ``trace`` mode additionally counts what it
        sees arrive, and a broker loops a publisher's own traffic back to
        it, so without this the same action would be counted twice. It is
        also what lets ``trace`` mode bound a burst at all: counting only
        inbound meant a tight loop with no await in between cleared every
        pre-check while the count was still zero, so the mode did not
        bound the one runaway it exists for.
        """
        if not trace_id or not self.limits.active or kind == "none":
            return
        if signal_id is not None:
            if signal_id in self._seen:
                return
            if len(self._seen) >= _MAX_SEEN:
                self._seen.pop(next(iter(self._seen)), None)
            self._seen[signal_id] = None
        t = self._tally(trace_id)
        t.actions += 1
        if kind == "tool_call":
            t.tool_calls += 1
        elif kind == "imprint":
            t.imprints += 1

    def over(self, trace_id: str) -> tuple[str, int, int] | None:
        """The first limit this trace has REACHED, as
        ``(name, count, limit)``, or None.

        Compared with ``>=`` because this is a pre-check: the caller asks
        before the action it is about to take, so a limit of 2 must allow
        exactly two and refuse the third.
        """
        if not trace_id or not self.limits.active:
            return None
        t = self._tallies.get(trace_id)
        if t is None:
            return None
        lim = self.limits
        for name, seen, cap in (
            ("max_tool_calls", t.tool_calls, lim.max_tool_calls),
            ("max_imprints", t.imprints, lim.max_imprints),
            ("max_actions", t.actions, lim.max_actions),
        ):
            if cap is not None and seen >= cap:
                return name, seen, cap
        return None

    def mark_refused(self, trace_id: str) -> bool:
        """Latch the first refusal on a trace. Returns True the first time,
        so the Dendrite escalates to ``stop_trace`` once rather than on
        every subsequent action."""
        if not trace_id:
            return False
        t = self._tally(trace_id)
        if t.refused:
            return False
        t.refused = True
        return True

    def forget(self, trace_id: str) -> None:
        self._tallies.pop(trace_id, None)

    def snapshot(self, trace_id: str) -> dict[str, int]:
        t = self._tallies.get(trace_id)
        if t is None:
            return {"actions": 0, "tool_calls": 0, "imprints": 0}
        return {
            "actions": t.actions, "tool_calls": t.tool_calls,
            "imprints": t.imprints,
        }


# ---------------------------------------------------------------------------
# Public introspection
# ---------------------------------------------------------------------------


def describe() -> dict[str, Any]:
    """The policy vocabulary, as data.

    Public and introspectable from day one so Genesis can render a card
    editor without hand-mirroring fields  -  the mistake ``_genesis_ast.py``
    exists to avoid repeating. Everything a card editor needs to build a
    valid artifact is reachable from here.
    """
    from cosmonapse.glia.artifact import RULE_FIELDS

    return {
        "directions": [d.value for d in Direction],
        "default_direction": Direction.BOTH.value,
        "signal_types": [t.value for t in SignalType],
        "retry": [r.value for r in Retry],
        "default_retry": Retry.NONE.value,
        "resendable": sorted(t.value for t in RESENDABLE),
        "reaskable_inbound": sorted(t.value for t in REASKABLE_INBOUND),
        "gates": {
            "axon": {
                "inbound": [
                    "TASK", "TOOL_RESULT", "RECALLED", "IMPRINTED",
                ],
                "outbound": [
                    "AGENT_OUTPUT", "CLARIFICATION", "PERMISSION", "ERROR",
                    "TOOL_CALL", "RECALL", "IMPRINT",
                ],
            },
            "effector": {"inbound": ["TOOL_CALL"], "outbound": ["TOOL_RESULT"]},
            "engram": {
                "inbound": ["RECALL", "IMPRINT"],
                "outbound": ["RECALLED", "IMPRINTED"],
            },
        },
        "verdicts": [d.value for d in Decision],
        "audit_kinds": {
            k.value: {
                "domain": AUDIT_DOMAIN[k],
                "is_retry": k not in (
                    AuditKind.GUARDED, AuditKind.LIMIT_REFUSED,
                    AuditKind.DEADLINE_ABANDONED,
                ),
            }
            for k in AuditKind
        },
        "audit_outcomes": [o.value for o in AuditOutcome],
        "audit_domains": sorted(set(AUDIT_DOMAIN.values())),
        "escalatable": sorted(
            f"{d.value}:{t.value}" for d, t in ESCALATABLE
        ),
        "modes": [m.value for m in Mode],
        "default_mode": Mode.OFF.value,
        "mode_env_var": MODE_ENV_VAR,
        "limit_modes": sorted(TraceLimits.MODES),
        "on_exceed": ["refuse", "stop_trace"],
        "meta_key": META_KEY,
        "bus_fields": sorted(BUS_FIELDS),
        "would_prefix": WOULD_PREFIX,
        "rule_fields": RULE_FIELDS,
        "card_fields": [
            "card_id", "issued_to", "issuer", "issued_at", "not_after",
            "version", "mode", "fail_open", "sample_pass", "journal",
            "repair", "limits", "policies",
        ],
        "not_built": {
            "identity": (
                "Phase 5: keypair, canonical form, sign, verify. Until an "
                "envelope carries a signature, policy is a developer "
                "convenience rather than governance."
            ),
            "trace_strict": (
                "Needs one authority for the count, so a round trip per "
                "decision. Not built."
            ),
        },
    }


# ---------------------------------------------------------------------------
# The callee-side gate: Effector and Engram
# ---------------------------------------------------------------------------


async def serve_gated(
    card: Glia | None,
    request: Signal,
    *,
    component: str,
    dendrite: Any,
    run: Callable[[Signal], Awaitable[Signal]],
    refuse: Callable[[Signal, PolicyOutcome], Signal],
) -> Signal:
    """Service one request through ``card``'s gate, both ways.

    The request is read inbound before ``run`` sees it; the reply ``run``
    builds is read outbound before it is returned for publishing. A
    violation emits one AUDIT record at the event and then does what its
    policy says: ``resend`` runs the request again, unchanged, up to the
    policy's ``max_attempts``; anything else answers with ``refuse``'s
    reply, the one the step would have produced anyway, with ``error``
    set. Never an ERROR (hard constraint 3), and never ``reask``: an
    Effector or an Engram has no generator to re-ask.

    With no card, or a card in ``off``, this is ``run(request)``.
    """
    # Servicing is not the caller's business. On an in-process synapse a
    # request is delivered inside the caller's own publish, so without this
    # anything the backend calls through the Dendrite would be gated as if
    # the calling Axon had sent it.
    with caller_gate(None):
        return await _serve_gated(
            card, request, component=component, dendrite=dendrite,
            run=run, refuse=refuse,
        )


async def _serve_gated(
    card: Glia | None,
    request: Signal,
    *,
    component: str,
    dendrite: Any,
    run: Callable[[Signal], Awaitable[Signal]],
    refuse: Callable[[Signal, PolicyOutcome], Signal],
) -> Signal:
    if card is None or card.mode is Mode.OFF:
        return await run(request)

    trace_id = request.trace_id
    parent_id = request.id
    record: dict[str, Any] = {}

    async def _audit(
        oc: PolicyOutcome, kind: AuditKind, outcome: AuditOutcome,
        attempt: int = 1,
    ) -> None:
        await audit_outcome(
            dendrite, oc, kind=kind, outcome=outcome, trace_id=trace_id,
            parent_id=parent_id, component=component, card=card,
            attempt=attempt,
        )

    oc = await card.check(
        request, direction=Direction.INBOUND, component=component,
    )
    if oc is not None:
        record.update(oc.record)
        if oc.shaped:
            if oc.denied or oc.escalated:
                await _audit(oc, AuditKind.GUARDED, AuditOutcome.REFUSED)
                return mark_exempt(with_glia_record(refuse(request, oc), record))
            if oc.redacted and isinstance(oc.replacement, dict):
                request = with_payload(request, oc.replacement)
                await _audit(oc, AuditKind.GUARDED, AuditOutcome.REDACTED)
        elif oc.declared.shapes:
            await _audit(oc, AuditKind.GUARDED, AuditOutcome.WOULD_BLOCK)

    resent = 0
    while True:
        reply = await run(request)
        oc = await card.check(
            reply, direction=Direction.OUTBOUND, component=component,
        )
        if oc is None:
            break
        record.update(oc.record)
        if not oc.shaped:
            if oc.declared.shapes:
                await _audit(oc, AuditKind.GUARDED, AuditOutcome.WOULD_BLOCK)
            break
        if oc.redacted and isinstance(oc.replacement, dict):
            reply = with_payload(reply, oc.replacement)
            await _audit(oc, AuditKind.GUARDED, AuditOutcome.REDACTED)
            break
        # A deny (an escalate here was already sanitised to one).
        retry = allowed_retry(
            oc.retry, Direction.OUTBOUND, reply.type, can_reask=False,
        )
        if retry is Retry.RESEND and resent < oc.max_attempts:
            resent += 1
            await _audit(
                oc, AuditKind.GUARD_RETRY, AuditOutcome.RESENT, attempt=resent,
            )
            continue
        if resent:
            await _audit(
                oc, AuditKind.GUARD_RETRY, AuditOutcome.EXHAUSTED,
                attempt=resent + 1,
            )
        else:
            await _audit(oc, AuditKind.GUARDED, AuditOutcome.REFUSED)
        reply = mark_exempt(refuse(request, oc))
        break

    if resent:
        record[F_ATTEMPT] = resent + 1
    return with_glia_record(reply, record)


# ---------------------------------------------------------------------------
# The caller-side gate: an Axon's own calls
# ---------------------------------------------------------------------------


class CallerGate:
    """An Axon's gate over the calls its Neuron makes and their replies.

    ``recall``, ``imprint`` and ``call_tool`` (through the injected
    helpers, ``axon.dendrite``, or the native tool channel) send a request
    and read a reply. The Effector and Engram clients hand both to this
    gate: the request outbound before it is published, the reply inbound
    before the caller sees it. It is bound for the length of one TASK by
    :func:`caller_gate`, so the Dendrite never reads a card; it only
    passes the gate through.

    A violation raises :class:`PolicyRefusal` into the code that made the
    call, after one AUDIT record, except for ``reask``: that record is
    written by the Axon's repair loop, which is the one place that knows
    whether the refusal was re-asked, and a refusal the Neuron swallowed
    is recorded when the pass ends (:meth:`flush`).
    """

    def __init__(
        self,
        card: Glia,
        *,
        component: str,
        dendrite: Any,
        trace_id: str,
        parent_id: str,
        can_reask: bool,
    ) -> None:
        self.card = card
        self.component = component
        self.dendrite = dendrite
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.can_reask = can_reask
        self._pending: list[PolicyRefusal] = []

    async def _audit(
        self, oc: PolicyOutcome, kind: AuditKind, outcome: AuditOutcome,
        attempt: int = 1,
    ) -> None:
        await audit_outcome(
            self.dendrite, oc, kind=kind, outcome=outcome,
            trace_id=self.trace_id, parent_id=self.parent_id,
            component=self.component, card=self.card, attempt=attempt,
        )

    async def outbound(self, signal: Signal) -> Signal:
        """Read a request leaving the Axon. Returns the signal to publish
        (a redacted copy, or the original with its record), or raises."""
        oc = await self.card.check(
            signal, direction=Direction.OUTBOUND, component=self.component,
        )
        if oc is None:
            return signal
        if not oc.shaped:
            if oc.declared.shapes:
                await self._audit(oc, AuditKind.GUARDED, AuditOutcome.WOULD_BLOCK)
            return with_glia_record(signal, oc.record)
        if oc.redacted and isinstance(oc.replacement, dict):
            await self._audit(oc, AuditKind.GUARDED, AuditOutcome.REDACTED)
            return with_glia_record(
                with_payload(signal, oc.replacement), oc.record,
            )
        retry = allowed_retry(
            oc.retry, Direction.OUTBOUND, signal.type,
            can_reask=self.can_reask,
        )
        # A resend of an outbound request repeats what was just denied, so
        # allowed_retry never returns it here; it is refused at load too.
        exc = PolicyRefusal(oc, retry=retry)
        if oc.escalated:
            await self._audit(oc, AuditKind.GUARDED, AuditOutcome.ESCALATED)
            exc.audited = True
        elif retry is Retry.REASK:
            self._pending.append(exc)
        else:
            await self._audit(oc, AuditKind.GUARDED, AuditOutcome.REFUSED)
            exc.audited = True
        raise exc

    async def inbound(
        self, signal: Signal, *, resent: int = 0, retry_allowed: bool = True,
    ) -> Signal | None:
        """Read a reply arriving at the Axon.

        Returns the signal the caller should see, None when the policy
        asks for the request to be resent (the caller sends it again and
        reads the new reply with ``resent`` one higher), or raises.
        """
        oc = await self.card.check(
            signal, direction=Direction.INBOUND, component=self.component,
        )
        if oc is None:
            return signal
        if not oc.shaped:
            if oc.declared.shapes:
                await self._audit(oc, AuditKind.GUARDED, AuditOutcome.WOULD_BLOCK)
            return with_glia_record(signal, oc.record)
        if oc.redacted and isinstance(oc.replacement, dict):
            await self._audit(oc, AuditKind.GUARDED, AuditOutcome.REDACTED)
            return with_glia_record(
                with_payload(signal, oc.replacement), oc.record,
            )
        retry = (
            allowed_retry(
                oc.retry, Direction.INBOUND, signal.type,
                can_reask=self.can_reask,
            )
            if retry_allowed else Retry.NONE
        )
        if retry is Retry.RESEND and resent < oc.max_attempts:
            await self._audit(
                oc, AuditKind.GUARD_RETRY, AuditOutcome.RESENT,
                attempt=resent + 1,
            )
            return None
        exc = PolicyRefusal(oc, retry=Retry.NONE if retry is Retry.RESEND else retry)
        if exc.retry is Retry.REASK:
            self._pending.append(exc)
        else:
            if resent:
                await self._audit(
                    oc, AuditKind.GUARD_RETRY, AuditOutcome.EXHAUSTED,
                    attempt=resent + 1,
                )
            else:
                await self._audit(oc, AuditKind.GUARDED, AuditOutcome.REFUSED)
            exc.audited = True
        raise exc

    async def flush(self, consumed: PolicyRefusal | None = None) -> None:
        """Record the ``reask`` refusals the repair loop did not take.

        A Neuron that catches a refusal and carries on has handled it, but
        the violation still happened, and the absence of an AUDIT on a
        trace has to mean nothing fired.
        """
        pending, self._pending = self._pending, []
        for exc in pending:
            if exc is consumed or exc.audited:
                continue
            exc.audited = True
            await self._audit(exc.outcome, AuditKind.GUARDED, AuditOutcome.REFUSED)


_CALLER_GATE: ContextVar[CallerGate | None] = ContextVar(
    "cosmonapse_caller_gate", default=None,
)


def current_caller_gate() -> CallerGate | None:
    """The gate for calls made from inside the current TASK, if any."""
    return _CALLER_GATE.get()


@contextmanager
def caller_gate(gate: CallerGate | None) -> Iterator[None]:
    """Bind ``gate`` for calls made inside this block."""
    token = _CALLER_GATE.set(gate)
    try:
        yield
    finally:
        _CALLER_GATE.reset(token)
