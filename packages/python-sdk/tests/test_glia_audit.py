"""The AUDIT audit stream.

One record per policy or repair event, on its own subject, carrying the
audit domain it rolls up to. A clean turn emits none, which is what makes
"no AUDIT on this trace" mean "nothing fired" rather than "nothing was
recorded".
"""

from __future__ import annotations

import asyncio
import json

import pytest

import cosmonapse as cn
from cosmonapse import (
    AuditKind,
    AuditOutcome,
    Axon,
    Dendrite,
    Directed,
    Effector,
    Engram,
    Glia,
    Hit,
    ImprintReceipt,
    MemorySynapse,
    SignalType,
    ToolOutcome,
    TraceLimitExceeded,
    Verdict,
    task_signal,
)
from cosmonapse.glia import AUDIT_DOMAIN, META_KEY, describe

NS = "audit"


class Bus:
    """A Dendrite with a AUDIT tap, which is how a compliance consumer
    would attach: one subject, nothing else."""

    def __init__(self, **dendrite_kwargs):
        self.synapse = MemorySynapse()
        self.dendrite = Dendrite(
            synapse=self.synapse, namespace=NS, **dendrite_kwargs,
        )
        self.retries: list = []
        self.all: list = []

    async def __aenter__(self):
        # Subscriptions before start, and components attached by the
        # caller before start: attach_axon refuses on a running Dendrite
        # because the TASK subscription is set up during start().
        await self.synapse.connect()

        async def on_audit(sig):
            self.retries.append(sig)

        async def on_any(sig):
            self.all.append(sig)

        await self.synapse.subscribe(f"cosmonapse.{NS}.AUDIT", on_audit)
        for st in (SignalType.AGENT_OUTPUT, SignalType.TOOL_RESULT,
                   SignalType.IMPRINTED, SignalType.RECALLED,
                   SignalType.PERMISSION, SignalType.ERROR):
            await self.synapse.subscribe(f"cosmonapse.{NS}.{st.value}", on_any)
        return self

    async def start(self):
        await self.dendrite.start()
        return self

    async def __aexit__(self, *exc):
        await asyncio.sleep(0.02)
        await self.dendrite.stop()

    def kinds(self):
        return [s.payload["kind"] for s in self.retries]

    def domains(self):
        return [s.payload["domain"] for s in self.retries]

    def outcomes(self):
        return [s.payload["outcome"] for s in self.retries]


def card(policies, **over):
    body = {"card_id": "claims-2026-09", "version": "4", "mode": "enforce",
            "policies": policies}
    body.update(over)
    return Glia.load(json.dumps(body))


NO_POLICY_NUMBER = {
    "id": "no-policy-number",
    "direction": "outbound", "types": ["AGENT_OUTPUT"],
    "verdict": "deny", "retry": "reask",
    "match": [r"\bPN-\d{8}\b"],
    "reason": "the draft must not quote a policy number",
}


def one_task(neuron_id="triage", **payload):
    return task_signal(directed=Directed(id=neuron_id), input=payload)


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_the_seven_kinds_and_their_audit_domains():
    assert [k.value for k in AuditKind] == [
        "GUARD_RETRY", "GUARDED", "EVAL_RETRY", "TOOL_RETRY",
        "TRANSIENT_RETRY", "LIMIT_REFUSED", "DEADLINE_ABANDONED",
    ]
    assert AUDIT_DOMAIN[AuditKind.GUARD_RETRY] == "security"
    assert AUDIT_DOMAIN[AuditKind.GUARDED] == "security"
    assert AUDIT_DOMAIN[AuditKind.EVAL_RETRY] == "eval"
    assert AUDIT_DOMAIN[AuditKind.TOOL_RETRY] == "tool"
    assert AUDIT_DOMAIN[AuditKind.TRANSIENT_RETRY] == "reliability"
    assert AUDIT_DOMAIN[AuditKind.LIMIT_REFUSED] == "governance"
    assert AUDIT_DOMAIN[AuditKind.DEADLINE_ABANDONED] == "reliability"


def test_the_three_that_are_not_retries_are_not_named_as_retries():
    """A security review that sees GUARDED or LIMIT_REFUSED must not be
    told something was retried when nothing was."""
    not_retries = {
        AuditKind.GUARDED, AuditKind.LIMIT_REFUSED,
        AuditKind.DEADLINE_ABANDONED,
    }
    for k in AuditKind:
        assert k.value.endswith("_RETRY") is (k not in not_retries), k

    published = describe()["audit_kinds"]
    for k in AuditKind:
        assert published[k.value]["is_retry"] is (k not in not_retries)
        assert published[k.value]["domain"] == AUDIT_DOMAIN[k]


def test_the_vocabulary_is_introspectable():
    d = describe()
    assert sorted(d["audit_domains"]) == [
        "eval", "governance", "reliability", "security", "tool",
    ]
    assert "re_asked" in d["audit_outcomes"]
    assert "would_block" in d["audit_outcomes"]


def test_retry_is_a_record_not_an_instruction():
    """It must resolve no wait, close no Pathway, and not be dropped by a
    terminal-scoped Pathway's own accounting."""
    from cosmonapse.pathway import (
        _SCOPE_TERMINAL_TYPES,
        _TERMINAL_TYPES,
        _WAIT_TYPES,
        PATHWAY_TYPES,
    )

    assert SignalType.AUDIT in PATHWAY_TYPES
    assert SignalType.AUDIT not in _TERMINAL_TYPES
    assert SignalType.AUDIT not in _WAIT_TYPES
    assert SignalType.AUDIT not in _SCOPE_TERMINAL_TYPES


def test_thought_delta_is_gone():
    assert not hasattr(SignalType, "THOUGHT_DELTA")
    assert not hasattr(cn, "thought_delta_signal")
    assert not hasattr(Dendrite, "emit_thought_delta")
    assert not hasattr(Dendrite, "on_thought_delta")


# ---------------------------------------------------------------------------
# The use case: pass, repaired, and failed
# ---------------------------------------------------------------------------


def _triage(leak_until):
    async def neuron(inp, ctx):
        attempt = (inp.get("repair") or {}).get("attempt", 0)
        if attempt >= leak_until:
            return {"draft": "Your claim is being reviewed."}
        return {"draft": "Your claim PN-40188322 is being reviewed."}
    return neuron


async def test_a_clean_turn_emits_no_retry_at_all():
    c = card([NO_POLICY_NUMBER], sample_pass=1)
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(0), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.retries == [], "nothing fired, so nothing is recorded"
    # And the pass is EXPLICIT on the reply, not inferred from the silence.
    assert reply.meta[META_KEY]["verdict"] == "allow"
    assert "attempts" not in reply.meta[META_KEY]


async def test_a_repaired_turn_emits_one_retry_per_refusal():
    c = card([NO_POLICY_NUMBER], repair={"max_attempts": 2})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(1), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.kinds() == ["GUARD_RETRY"]
    assert bus.domains() == ["security"]
    assert bus.outcomes() == ["re_asked"]
    p = bus.retries[0].payload
    assert p["component"] == "triage"
    assert p["direction"] == "outbound"
    assert p["signal"] == "AGENT_OUTPUT"
    assert p["policy_id"] == "no-policy-number"
    assert p["policy_version"] == "4"
    assert p["reason"] == "the draft must not quote a policy number"
    # The reply still carries the authoritative verdict.
    assert reply.payload["output"] == {"draft": "Your claim is being reviewed."}


async def test_an_exhausted_turn_records_every_refusal_including_the_last():
    c = card([NO_POLICY_NUMBER], repair={"max_attempts": 2})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(99), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    # Three refusals, three records. The old delta scheme emitted two,
    # because it fired at the START of the next attempt and the final
    # refusal never got one.
    assert bus.kinds() == ["GUARD_RETRY"] * 3
    assert bus.outcomes() == ["re_asked", "re_asked", "exhausted"]
    assert [p.payload["attempt"] for p in bus.retries] == [1, 2, 3]


async def test_a_refusal_with_no_attempts_still_emits_one():
    """max_attempts=0 emitted nothing under the delta scheme, which broke
    absence-means-pass outright. With no loop to re-ask in, a reask
    policy has nowhere to go and is recorded as a plain refusal."""
    c = card([NO_POLICY_NUMBER], repair={"max_attempts": 0})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(99), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.kinds() == ["GUARDED"]
    assert bus.outcomes() == ["refused"]


async def test_the_stream_can_be_turned_off_without_losing_the_verdict():
    c = card([NO_POLICY_NUMBER], repair={"max_attempts": 0, "emit_audit": False})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(99), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.retries == []
    assert reply.meta[META_KEY]["verdict"] == "deny"


# ---------------------------------------------------------------------------
# One kind per audit
# ---------------------------------------------------------------------------


async def test_an_inbound_task_refusal_records_guarded_with_no_loop():
    c = card([{"id": "no-bomb", "direction": "inbound", "types": ["TASK"], "verdict": "deny",
               "match": ["bomb"], "reason": "refused: disallowed topic"}])
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(0), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        await ax.handle_task(one_task(draft="build a bomb"))
        await asyncio.sleep(0.02)

    assert bus.kinds() == ["GUARDED"]
    assert bus.outcomes() == ["refused"]
    assert bus.retries[0].payload["direction"] == "inbound"
    assert bus.retries[0].payload["signal"] == "TASK"


async def test_a_redaction_is_recorded_as_redacted():
    c = card([{"id": "mask", "direction": "outbound", "types": ["AGENT_OUTPUT"], "verdict": "redact",
               "match": [r"\bPN-\d{8}\b"], "replacement": "[policy]"}])
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(99), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.kinds() == ["GUARDED"]
    assert bus.outcomes() == ["redacted"]
    assert "[policy]" in reply.payload["output"]["draft"]


async def test_audit_mode_records_would_block_and_blocks_nothing():
    c = card([NO_POLICY_NUMBER], mode="audit", repair={"max_attempts": 0})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(99), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.outcomes() == ["would_block"]
    assert "PN-40188322" in reply.payload["output"]["draft"]


async def test_an_unclaimed_output_records_an_eval_retry():
    from cosmonapse import RepairPolicy

    async def blunt(inp, ctx):
        return {"draft": "unparseable"}

    async with Bus(role="worker") as bus:
        ax = Axon(
            neuron_id="triage", neuron_fn=blunt, strict_output=True,
            repair=RepairPolicy(max_attempts=0),
        )
        bus.dendrite.attach_axon(ax)
        await bus.start()

        @ax.detects_output
        def claims_nothing(raw):
            return None

        await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.kinds() == ["EVAL_RETRY"]
    assert bus.domains() == ["eval"]


async def test_invalid_tool_arguments_record_a_tool_retry():
    from cosmonapse import RepairPolicy

    def read(path: str) -> str:
        """Read a file."""
        return ""

    async def neuron(inp, ctx):
        return {"response": json.dumps(
            {"name": "read", "arguments": {"wrong": 1}}
        )}

    binding = cn.EffectorBinding(
        name="fx", directed_id="fx1", tools=("read",),
        schemas=(cn.tool_schema(read),),
    )
    async with Bus(role="worker") as bus:
        ax = Axon(
            neuron_id="triage", neuron_fn=neuron, tool_standard="codex",
            effectors=[binding], repair=RepairPolicy(max_attempts=0),
        )
        bus.dendrite.attach_axon(ax)
        await bus.start()
        await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.kinds() == ["TOOL_RETRY"]
    assert bus.domains() == ["tool"]


async def test_a_callee_side_refusal_records_guarded():
    c = card([{"id": "no-rm", "types": ["TOOL_CALL"],
               "verdict": "deny", "tools": ["shell"], "match": [r"rm\s+-rf"],
               "reason": "no recursive delete"}])
    fx = Effector.serve(effector_id="fx1", effector_kind="shell", glia=c)

    @fx.on_tool_call
    async def run(tool, args):  # pragma: no cover - refused first
        return {"ran": True}

    async with Bus() as bus:
        bus.dendrite.attach_effector(fx)
        await bus.start()
        await bus.synapse.publish(f"cosmonapse.{NS}.TOOL_CALL", cn.tool_call_signal(
            trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
            tool="shell", args={"cmd": "rm -rf /"}, directed=Directed(id="fx1"),
        ))
        await asyncio.sleep(0.05)

    assert bus.kinds() == ["GUARDED"]
    assert bus.outcomes() == ["refused"]
    assert bus.retries[0].payload["component"] == "fx1"
    assert bus.retries[0].payload["direction"] == "inbound"
    assert bus.retries[0].payload["signal"] == "TOOL_CALL"


async def test_a_transient_fault_records_a_reliability_retry():
    c = card([], repair={"transient_attempts": 2, "transient_backoff_s": 0})

    class Flaky(Effector):
        effector_id = "flaky"
        effector_kind = "custom"
        tries = 0

        def __init__(self) -> None:
            self.capabilities = ["go"]

        async def connect(self): ...
        async def close(self): ...

        async def invoke(self, tool, args, *, call_id=None, deadline_ms=None,
                         trace_id=None):
            Flaky.tries += 1
            if Flaky.tries < 2:
                raise ConnectionResetError("dropped connection")
            return ToolOutcome(tool=tool, result={"ok": True})

    fx = Flaky()
    async with Bus() as bus:
        bus.dendrite.attach_effector(fx, glia=c)
        await bus.start()
        await bus.synapse.publish(f"cosmonapse.{NS}.TOOL_CALL", cn.tool_call_signal(
            trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
            tool="go", args={}, directed=Directed(id="flaky"),
        ))
        await asyncio.sleep(0.08)

    assert bus.kinds() == ["TRANSIENT_RETRY"]
    assert bus.domains() == ["reliability"]
    assert bus.outcomes() == ["recovered"]


async def test_a_trace_limit_records_a_governance_refusal():
    c = Glia.load(json.dumps({
        "card_id": "d", "mode": "enforce",
        "limits": {"mode": "component", "max_actions": 1},
    }))
    async with Bus(glia=c) as bus:
        await bus.start()
        trace, parent = cn.new_trace_id(), cn.new_event_id()
        await bus.dendrite.emit_plan(trace_id=trace, parent_id=parent, steps=[])
        with pytest.raises(TraceLimitExceeded):
            await bus.dendrite.emit_plan(
                trace_id=trace, parent_id=parent, steps=[],
            )
        await asyncio.sleep(0.05)

    assert bus.kinds() == ["LIMIT_REFUSED"]
    assert bus.domains() == ["governance"]


async def test_the_limit_does_not_suppress_its_own_audit_record():
    """A AUDIT counts as nothing against a trace limit. Otherwise a trace
    at its cap would spend its remaining budget on records of being at
    its cap, and then go silent."""
    c = Glia.load(json.dumps({
        "card_id": "d", "mode": "enforce",
        "limits": {"mode": "component", "max_actions": 1},
    }))
    async with Bus(glia=c) as bus:
        await bus.start()
        trace, parent = cn.new_trace_id(), cn.new_event_id()
        await bus.dendrite.emit_plan(trace_id=trace, parent_id=parent, steps=[])
        for _ in range(3):
            with pytest.raises(TraceLimitExceeded):
                await bus.dendrite.emit_plan(
                    trace_id=trace, parent_id=parent, steps=[],
                )
        await asyncio.sleep(0.05)
        snapshot = bus.dendrite.trace_counter.snapshot(trace)

    assert snapshot["actions"] == 1, "the audit records did not consume budget"
    assert bus.retries, "and the records got out"


async def test_the_loop_abandons_itself_before_the_caller_times_out():
    async def slow_and_stubborn(inp, ctx):
        await asyncio.sleep(0.05)
        return {"draft": "Your claim PN-40188322 is being reviewed."}

    c = card([NO_POLICY_NUMBER],
             repair={"max_attempts": 9, "deadline_s": 0.01})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=slow_and_stubborn, glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.kinds() == ["DEADLINE_ABANDONED"]
    assert bus.domains() == ["reliability"]
    assert bus.outcomes() == ["exhausted"]


# ---------------------------------------------------------------------------
# What must never ride a AUDIT
# ---------------------------------------------------------------------------


async def test_a_retry_never_carries_the_matched_content(tmp_path):
    jpath = tmp_path / "glia.jsonl"
    c = card([NO_POLICY_NUMBER], journal={"path": str(jpath)},
             repair={"max_attempts": 0})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(99), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    blob = json.dumps([s.payload for s in bus.retries])
    assert "PN-40188322" not in blob
    # ... and the hash is what joins the record to the journal line that
    # does carry it.
    from cosmonapse.glia import Journal

    entries = Journal(jpath).read_all()
    assert entries[-1]["matched"] == "PN-40188322"
    assert bus.retries[0].payload["hash"] == entries[-1]["hash"]


async def test_an_escalation_records_escalated_not_refused():
    c = Glia(card_id="c", mode="enforce")

    @c.on_signal(direction="outbound", types=["AGENT_OUTPUT"])
    def review(sig):
        return Verdict.escalate("a human should approve this draft")

    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=_triage(0), glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert bus.outcomes() == [AuditOutcome.ESCALATED.value]
    assert reply.type is SignalType.PERMISSION


async def test_an_engram_recall_masks_rows_and_records_it():
    c = card([{"id": "ssn", "direction": "outbound", "types": ["RECALLED"],
               "verdict": "redact", "keys": ["ssn"],
               "reason": "ssn never leaves the engram"}])
    eg = Engram.serve(engram_id="eg1", glia=c)

    @eg.on_recall
    async def read(query):
        return [Hit(id="1", entry={"ssn": "123-45", "name": "ada"})]

    @eg.on_imprint
    async def write(op, entry):
        return ImprintReceipt(engram_id="eg1", op=op, id="x")

    reply = await eg.handle(cn.recall_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        query={"q": "ada"}, directed=Directed(id="eg1"),
    ))
    assert [h["entry"] for h in reply.payload["hits"]] == [{"name": "ada"}]
    assert reply.meta[META_KEY]["verdict"] == "redact"
    assert reply.meta[META_KEY]["signal"] == "RECALLED"


# ---------------------------------------------------------------------------
# Retry per policy, and its records
# ---------------------------------------------------------------------------


def _shell(results, calls):
    fx = Effector.serve(effector_id="fx1", effector_kind="shell")
    queue = list(results)

    @fx.on_tool_call
    async def run(tool, args):
        calls.append(dict(args))
        return queue.pop(0) if queue else {"s": "stale"}

    return fx


FXB = cn.EffectorBinding(name="fx", directed_id="fx1", tools=("shell",),
                         default_deadline_ms=2000)


async def test_retry_none_refuses_at_once_and_records_guarded():
    runs = []

    async def neuron(inp, ctx):
        runs.append(1)
        return {"draft": "Your claim PN-40188322 is being reviewed."}

    c = card([{**NO_POLICY_NUMBER, "retry": "none"}],
             repair={"max_attempts": 3})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=neuron, glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert runs == [1], "a policy that asks for no retry is not re-asked"
    assert bus.kinds() == ["GUARDED"]
    assert bus.outcomes() == ["refused"]
    assert reply.payload["output"]["error"] == NO_POLICY_NUMBER["reason"]


async def test_a_policy_budget_below_the_card_ceiling_wins():
    runs = []

    async def neuron(inp, ctx):
        runs.append(1)
        return {"draft": "Your claim PN-40188322 is being reviewed."}

    c = card([{**NO_POLICY_NUMBER, "max_attempts": 1}],
             repair={"max_attempts": 5})
    async with Bus(role="worker") as bus:
        ax = Axon(neuron_id="triage", neuron_fn=neuron, glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert len(runs) == 2, "the first pass and the one re-ask the policy allows"
    assert bus.outcomes() == ["re_asked", "exhausted"]


async def test_an_inbound_reply_can_be_re_asked():
    """A TOOL_RESULT the Axon refuses goes back to the Neuron as a
    correction, and the Neuron's next attempt makes a different call."""
    calls: list = []

    async def neuron(inp, ctx, *, call_tool):
        attempt = (inp.get("repair") or {}).get("attempt", 0)
        cmd = "status --fresh" if attempt else "status"
        outcome = await call_tool("fx", tool="shell", args={"cmd": cmd})
        return {"draft": f"state is {outcome.result['s']}"}

    c = card([{"id": "no-stale", "direction": "inbound",
               "types": ["TOOL_RESULT"], "verdict": "deny",
               "match": ["stale"], "retry": "reask",
               "reason": "that result is stale; ask for a fresh one"}],
             repair={"max_attempts": 2})
    async with Bus(role="worker") as bus:
        bus.dendrite.attach_effector(_shell([{"s": "stale"}, {"s": "fresh"}], calls))
        ax = Axon(neuron_id="triage", neuron_fn=neuron, effectors=[FXB],
                  tool_standard="codex", glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert [c_["cmd"] for c_ in calls] == ["status", "status --fresh"]
    assert reply.payload["output"] == {"draft": "state is fresh"}
    assert bus.kinds() == ["GUARD_RETRY"]
    assert bus.outcomes() == ["re_asked"]
    assert bus.retries[0].payload["direction"] == "inbound"
    assert bus.retries[0].payload["signal"] == "TOOL_RESULT"


async def test_a_resend_records_each_resend_and_the_end():
    calls: list = []

    async def neuron(inp, ctx, *, call_tool):
        await call_tool("fx", tool="shell", args={"cmd": "status"})
        return {"draft": "not reached"}

    c = card([{"id": "no-stale", "direction": "inbound",
               "types": ["TOOL_RESULT"], "verdict": "deny",
               "match": ["stale"], "retry": "resend", "max_attempts": 2,
               "reason": "stale"}])
    async with Bus(role="worker") as bus:
        bus.dendrite.attach_effector(_shell([], calls))
        ax = Axon(neuron_id="triage", neuron_fn=neuron, effectors=[FXB],
                  tool_standard="codex", glia=c)
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert len(calls) == 3
    assert bus.kinds() == ["GUARD_RETRY", "GUARD_RETRY", "GUARD_RETRY"]
    assert bus.outcomes() == ["resent", "resent", "exhausted"]
    assert reply.payload["output"]["error"] == "stale"


async def test_a_refusal_the_neuron_swallowed_is_still_recorded():
    """The Neuron caught the refusal and carried on. The violation still
    happened, and no AUDIT on a trace has to mean nothing fired."""

    async def neuron(inp, ctx, *, imprint):
        try:
            await imprint("mem", op="add", entry={"password": "hunter2"})
        except cn.PolicyRefusal:
            pass
        return {"draft": "done"}

    c = card([{"id": "no-creds", "direction": "outbound",
               "types": ["IMPRINT"], "verdict": "deny", "retry": "reask",
               "match": ["password"], "reason": "no credentials"}],
             repair={"max_attempts": 2})
    eg = Engram.serve(engram_id="eg1")

    @eg.on_imprint
    async def write(op, entry):  # pragma: no cover - refused first
        return ImprintReceipt(engram_id="eg1", op=op, id="x")

    async with Bus(role="worker") as bus:
        bus.dendrite.attach_engram(eg)
        ax = Axon(neuron_id="triage", neuron_fn=neuron, glia=c,
                  engrams=[cn.EngramBinding(name="mem", directed_id="eg1")])
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert reply.payload["output"] == {"draft": "done"}
    assert bus.kinds() == ["GUARDED"]
    assert bus.outcomes() == ["refused"]
    assert bus.retries[0].payload["signal"] == "IMPRINT"


async def test_an_unswallowed_reask_refusal_is_recorded_once_by_the_loop():
    runs = []

    async def neuron(inp, ctx, *, imprint):
        runs.append(1)
        if not (inp.get("repair") or {}):
            await imprint("mem", op="add", entry={"password": "hunter2"})
        return {"draft": "done"}

    c = card([{"id": "no-creds", "direction": "outbound",
               "types": ["IMPRINT"], "verdict": "deny", "retry": "reask",
               "match": ["password"], "reason": "no credentials"}],
             repair={"max_attempts": 2})
    eg = Engram.serve(engram_id="eg1")

    @eg.on_imprint
    async def write(op, entry):  # pragma: no cover - refused first
        return ImprintReceipt(engram_id="eg1", op=op, id="x")

    async with Bus(role="worker") as bus:
        bus.dendrite.attach_engram(eg)
        ax = Axon(neuron_id="triage", neuron_fn=neuron, glia=c,
                  engrams=[cn.EngramBinding(name="mem", directed_id="eg1")])
        bus.dendrite.attach_axon(ax)
        await bus.start()
        reply = await ax.handle_task(one_task())
        await asyncio.sleep(0.02)

    assert runs == [1, 1]
    assert reply.payload["output"] == {"draft": "done"}
    assert bus.kinds() == ["GUARD_RETRY"], "one event, one record"
    assert bus.outcomes() == ["re_asked"]


async def test_an_effector_resend_records_each_resend():
    c = card([{"id": "no-empty", "direction": "outbound",
               "types": ["TOOL_RESULT"], "verdict": "deny",
               "match": ['"rows": \\[\\]'], "retry": "resend",
               "max_attempts": 3}])
    fx = Effector.serve(effector_id="fx1", effector_kind="shell", glia=c)
    queue = [{"rows": []}, {"rows": []}, {"rows": [1]}]

    @fx.on_tool_call
    async def run(tool, args):
        return queue.pop(0)

    async with Bus() as bus:
        bus.dendrite.attach_effector(fx)
        await bus.start()
        await bus.synapse.publish(f"cosmonapse.{NS}.TOOL_CALL", cn.tool_call_signal(
            trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
            tool="shell", args={}, directed=Directed(id="fx1"),
        ))
        await asyncio.sleep(0.05)

    assert bus.kinds() == ["GUARD_RETRY", "GUARD_RETRY"]
    assert bus.outcomes() == ["resent", "resent"]
    assert all(r.payload["component"] == "fx1" for r in bus.retries)
    assert all(r.payload["direction"] == "outbound" for r in bus.retries)
