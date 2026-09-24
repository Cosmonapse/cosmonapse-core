"""
cosmonapse.glia
~~~~~~~~~~~~~~~
Glia: a thin two-way policy gate around each component.

An Axon, an Effector or an Engram may carry one card: a portable object
holding policies, inserted into the component rather than written into
it. The card reads every signal entering and leaving its component, and
refuses, rewrites or escalates it. A violation emits an AUDIT record and
then retries as the policy says: re-ask the Neuron, resend the request,
or refuse.

Decentralisation first. There is no orchestrator and no central policy
process, and a card is never consulted over the wire at request time.
Centralisation, if a deployment wants it, is a count of cards and a
choice of topology at deploy time, not a mode in the SDK  -  which is
why there is no ``centralized`` flag anywhere in this package.

    from cosmonapse.glia import Glia, Verdict

    CARD = Glia.load("corp.card")

    @CARD.on_signal(direction="outbound", types=["TOOL_CALL"], retry="reask")
    def no_writes_after_hours(sig):
        if sig.payload.get("tool") == "write" and _after_hours():
            return Verdict.deny("writes are closed outside business hours")
        return None

    Axon(..., glia=CARD)

Mode defaults to ``off``, so mounting a card changes nothing until the
deployment asks for ``audit`` and then ``enforce``. An empty card does
nothing at all: cosmonapse ships the seam, not a PII regex library or an
injection detector.

Not built here, deliberately:

* ``identity`` (Phase 5)  -  keypair, canonical form, sign, verify.
  Until an envelope carries a signature, any participant can publish as
  any ``directed.id``, so policy is a developer convenience rather than
  governance. See design/GLIA_DESIGN.md section 11.
* ``trace_strict`` trace limits  -  they need one authority for the
  count, so a round trip per decision.
"""

from cosmonapse.glia.artifact import RULE_FIELDS, PolicyArtifact, PolicyRule
from cosmonapse.glia.base import (
    AUDIT_DOMAIN,
    BUS_FIELDS,
    ESCALATABLE,
    F_ATTEMPT,
    F_ATTEMPTS,
    F_CARD_HASH,
    F_CARD_ID,
    F_COMPONENT,
    F_DIRECTION,
    F_HASH,
    F_ISSUED_AT,
    F_LIMIT,
    F_MODE,
    F_POLICY_ID,
    F_POLICY_VERSION,
    F_RETRY,
    F_SIGNAL,
    F_VERDICT,
    KIND_FOR_TRIGGER,
    META_KEY,
    MODE_ENV_VAR,
    OUTCOME_FOR_DECISION,
    REASKABLE_INBOUND,
    REGISTER_FIELDS,
    RESENDABLE,
    WOULD_PREFIX,
    AuditKind,
    AuditOutcome,
    CallerGate,
    Decision,
    Direction,
    Glia,
    Mode,
    PolicyError,
    PolicyOutcome,
    PolicyRefusal,
    RepairPolicy,
    Retry,
    SignalView,
    TraceCounter,
    TraceLimits,
    Verdict,
    allowed_retry,
    audit_outcome,
    caller_gate,
    canonical_text,
    coerce_types,
    content_hash,
    current_caller_gate,
    describe,
    is_exempt,
    mark_exempt,
    outcome_for,
    publish_audit,
    redact_text_leaves,
    register_meta,
    run_with_transient_retry,
    serve_gated,
    with_glia_record,
    with_payload,
)
from cosmonapse.glia.journal import Journal

__all__ = [
    "AUDIT_DOMAIN",
    "BUS_FIELDS",
    "ESCALATABLE",
    "F_ATTEMPT",
    "F_ATTEMPTS",
    "F_CARD_HASH",
    "F_CARD_ID",
    "F_COMPONENT",
    "F_DIRECTION",
    "F_HASH",
    "F_ISSUED_AT",
    "F_LIMIT",
    "F_MODE",
    "F_POLICY_ID",
    "F_POLICY_VERSION",
    "F_RETRY",
    "F_SIGNAL",
    "F_VERDICT",
    "KIND_FOR_TRIGGER",
    "META_KEY",
    "MODE_ENV_VAR",
    "OUTCOME_FOR_DECISION",
    "REASKABLE_INBOUND",
    "REGISTER_FIELDS",
    "RESENDABLE",
    "RULE_FIELDS",
    "WOULD_PREFIX",
    "AuditKind",
    "AuditOutcome",
    "CallerGate",
    "Decision",
    "Direction",
    "Glia",
    "Journal",
    "Mode",
    "PolicyArtifact",
    "PolicyError",
    "PolicyOutcome",
    "PolicyRefusal",
    "PolicyRule",
    "RepairPolicy",
    "Retry",
    "SignalView",
    "TraceCounter",
    "TraceLimits",
    "Verdict",
    "allowed_retry",
    "audit_outcome",
    "caller_gate",
    "canonical_text",
    "coerce_types",
    "content_hash",
    "current_caller_gate",
    "describe",
    "is_exempt",
    "mark_exempt",
    "outcome_for",
    "publish_audit",
    "redact_text_leaves",
    "register_meta",
    "run_with_transient_retry",
    "serve_gated",
    "with_glia_record",
    "with_payload",
]
