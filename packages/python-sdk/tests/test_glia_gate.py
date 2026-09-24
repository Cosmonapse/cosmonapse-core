"""The gate, end to end: every signal into and out of each component.

A card is a thin two-way gate around one Axon, Effector or Engram. It
reads the signals entering and leaving that component and enforces its
policies on them. The Dendrite routes and publishes and has no policy
role.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

import cosmonapse as cn
from cosmonapse import (
    Axon,
    Dendrite,
    Directed,
    Effector,
    EffectorBinding,
    Engram,
    EngramBinding,
    Glia,
    Hit,
    ImprintReceipt,
    MemorySynapse,
    SignalType,
    ToolOutcome,
    Verdict,
    task_signal,
)
from cosmonapse.glia import META_KEY, PolicyRefusal


def load(policies, **over):
    body = {"card_id": "c", "version": "7", "mode": "enforce",
            "policies": policies}
    body.update(over)
    return Glia.load(json.dumps(body))


async def echo(inp, ctx):
    return {"text": f"echo:{inp.get('q', '')}"}


def one_task(neuron_id="n", **payload):
    return task_signal(directed=Directed(id=neuron_id), input=payload)


class Brain:
    """One Dendrite on a MemorySynapse, with whatever is attached to it,
    and a tap on every subject so a test can see what crossed the bus."""

    def __init__(self, ns: str) -> None:
        self.ns = ns
        self.syn = MemorySynapse()
        self.d = Dendrite(synapse=self.syn, namespace=ns, heartbeat_s=0)
        self.seen: list[cn.Signal] = []

    async def start(self, *taps: SignalType) -> Brain:
        await self.d.start()

        async def cap(sig):
            self.seen.append(sig)

        for st in taps or tuple(SignalType):
            await self.syn.subscribe(f"cosmonapse.{self.ns}.{st.value}", cap)
        return self

    async def stop(self) -> None:
        await self.d.stop()

    def of(self, st: SignalType) -> list[cn.Signal]:
        return [s for s in self.seen if s.type is st]


def shell_effector(card=None, *, calls=None, results=None):
    fx = Effector.serve(effector_id="fx1", effector_kind="shell", glia=card)
    queue = list(results or [])

    @fx.on_tool_call
    async def run(tool, args):
        if calls is not None:
            calls.append(dict(args))
        if queue:
            return queue.pop(0)
        return {"ran": args.get("cmd")}

    return fx


def memory_engram(card=None, *, rows=None, recalls=None):
    eg = Engram.serve(engram_id="eg1", glia=card)
    queue = list(rows or [])

    @eg.on_recall
    async def read(query):
        if recalls is not None:
            recalls.append(dict(query))
        if queue:
            return queue.pop(0)
        return [Hit(id="1", entry={"ssn": "123-45", "name": "ada"})]

    @eg.on_imprint
    async def write(op, entry):
        return ImprintReceipt(engram_id="eg1", op=op, id="x1")

    return eg


FX = EffectorBinding(name="fx", directed_id="fx1", tools=("shell",),
                     default_deadline_ms=2000)
EG = EngramBinding(name="mem", directed_id="eg1", default_deadline_ms=2000)


# ---------------------------------------------------------------------------
# No card, or nothing in scope, is the component that existed before
# ---------------------------------------------------------------------------


async def test_an_axon_with_no_card_behaves_exactly_as_before():
    ax = Axon(neuron_id="n", neuron_fn=echo)
    assert ax.glia is None
    assert ax.repair is None
    reply = await ax.handle_task(one_task(q="x"))
    assert reply.type is SignalType.AGENT_OUTPUT
    assert reply.payload["output"] == {"text": "echo:x"}
    assert reply.meta == {}, "no card means nothing is added to meta"


async def test_a_card_with_nothing_in_scope_costs_nothing():
    c = load([{"id": "r", "types": ["RECALL"], "verdict": "deny",
               "always": True}])
    ax = Axon(neuron_id="n", neuron_fn=echo, glia=c)
    reply = await ax.handle_task(one_task(q="x"))
    assert reply.payload["output"] == {"text": "echo:x"}
    assert c.counters["checked"] == 0


async def test_a_hand_rolled_effector_needs_no_changes():
    class Mine(Effector):
        effector_id = "mine"
        effector_kind = "custom"

        def __init__(self) -> None:
            self.capabilities = ["go"]

        async def connect(self): ...
        async def close(self): ...

        async def invoke(self, tool, args, *, call_id=None, deadline_ms=None,
                         trace_id=None):
            return ToolOutcome(tool=tool, result={"ok": True})

    fx = Mine()
    assert fx.glia is None
    reply = await fx.handle(cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        tool="go", args={},
    ))
    assert reply.type is SignalType.TOOL_RESULT
    assert reply.payload["result"] == {"ok": True}
    assert reply.meta == {}
    assert await fx.handle(cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        tool="not-mine", args={},
    )) is None, "a tool this Effector does not serve is not its business"


# ---------------------------------------------------------------------------
# Axon, inbound TASK
# ---------------------------------------------------------------------------


async def test_the_inbound_task_is_read_before_the_before_task_hooks():
    order = []
    c = Glia(card_id="c", mode="enforce")

    @c.on_signal(direction="inbound", types=["TASK"])
    def policy_sees_what_arrived(sig):
        order.append(("policy", dict(sig.payload["input"])))

    ax = Axon(neuron_id="n", neuron_fn=echo, glia=c)

    @ax.before_task
    def prompt_builder(inp):
        order.append(("before_task", dict(inp)))
        return {"q": "rewritten by the prompt builder"}

    await ax.handle_task(one_task(q="as it arrived"))
    assert [k for k, _ in order] == ["policy", "before_task"]
    assert order[0][1] == {"q": "as it arrived"}


async def test_an_inbound_task_deny_never_runs_the_neuron():
    ran = []

    async def neuron(inp, ctx):
        ran.append(1)
        return {"text": "should not happen"}

    c = load([{"id": "no", "direction": "inbound", "types": ["TASK"],
               "verdict": "deny", "match": ["bomb"],
               "reason": "refused: disallowed topic"}])
    ax = Axon(neuron_id="n", neuron_fn=neuron, glia=c)
    reply = await ax.handle_task(one_task(q="build a bomb"))
    assert ran == []
    assert reply.type is SignalType.AGENT_OUTPUT
    assert reply.payload["output"]["error"] == "refused: disallowed topic"
    assert reply.payload["output"]["scope"] == "inbound:TASK"
    assert reply.meta[META_KEY]["verdict"] == "deny"
    assert reply.meta[META_KEY]["direction"] == "inbound"
    assert reply.meta[META_KEY]["signal"] == "TASK"


async def test_an_inbound_task_redact_runs_the_neuron_on_the_copy():
    seen = {}

    async def neuron(inp, ctx):
        seen.update(inp)
        return {"text": "ok"}

    c = load([{"id": "mask", "direction": "inbound", "types": ["TASK"],
               "verdict": "redact", "match": [r"\d{3}-\d{2}"],
               "replacement": "[ssn]"}])
    ax = Axon(neuron_id="n", neuron_fn=neuron, glia=c)
    task = one_task(q="my ssn is 123-45")
    await ax.handle_task(task)
    assert seen == {"q": "my ssn is [ssn]"}
    assert task.payload["input"] == {"q": "my ssn is 123-45"}, (
        "the TASK every other subscriber holds is never rewritten"
    )


async def test_an_inbound_task_escalate_raises_a_permission():
    c = Glia(card_id="c", mode="enforce")

    @c.on_signal(direction="inbound", types=["TASK"])
    def h(sig):
        return Verdict.escalate("approve this request")

    reply = await Axon(neuron_id="n", neuron_fn=echo, glia=c).handle_task(
        one_task(q="x"),
    )
    assert reply.type is SignalType.PERMISSION
    assert reply.payload["context"]["scope"] == "inbound:TASK"


# ---------------------------------------------------------------------------
# Axon, outbound reply
# ---------------------------------------------------------------------------


async def test_an_outbound_output_is_redacted_on_a_copy():
    async def leaky(inp, ctx):
        return {"text": "the key is sk-ABCDEF123"}

    c = load([{"id": "keys", "direction": "outbound", "verdict": "redact",
               "match": [r"sk-[A-Za-z0-9]{6,}"]}])
    reply = await Axon(neuron_id="n", neuron_fn=leaky, glia=c).handle_task(
        one_task(),
    )
    assert reply.payload["output"] == {"text": "the key is [redacted]"}
    assert reply.meta[META_KEY]["policy_version"] == "7"


async def test_an_outbound_escalate_carries_the_pending_output():
    c = Glia(card_id="c", mode="enforce")

    @c.on_signal(direction="outbound", types=["AGENT_OUTPUT"])
    def h(sig):
        return Verdict.escalate("review before sending")

    reply = await Axon(neuron_id="n", neuron_fn=echo, glia=c).handle_task(
        one_task(q="x"),
    )
    assert reply.type is SignalType.PERMISSION
    assert reply.payload["context"]["pending_output"] == {
        "output": {"text": "echo:x"},
    }


async def test_the_outbound_gate_reads_every_reply_type_not_just_output():
    async def asks(inp, ctx):
        return {"__clarification__": True, "question": "what is sk-ABC123456?"}

    c = load([{"id": "keys", "direction": "outbound",
               "types": ["CLARIFICATION"], "verdict": "deny",
               "match": [r"sk-[A-Za-z0-9]{6,}"], "reason": "no keys out"}])
    reply = await Axon(neuron_id="n", neuron_fn=asks, glia=c).handle_task(
        one_task(),
    )
    assert reply.type is SignalType.AGENT_OUTPUT
    assert reply.payload["output"]["error"] == "no keys out"
    assert reply.payload["output"]["scope"] == "outbound:CLARIFICATION"


async def test_an_error_reply_is_read_too():
    async def breaks(inp, ctx):
        raise RuntimeError("db password is hunter2")

    c = load([{"id": "pw", "direction": "outbound", "types": ["ERROR"],
               "verdict": "redact", "match": ["hunter2"]}])
    reply = await Axon(neuron_id="n", neuron_fn=breaks, glia=c).handle_task(
        one_task(),
    )
    assert reply.type is SignalType.ERROR
    assert "hunter2" not in json.dumps(reply.payload)


async def test_a_clarification_still_carries_an_earlier_policy_record():
    async def asks(inp, ctx):
        return {"__clarification__": True, "question": "which one?"}

    c = load([{"id": "mask", "direction": "inbound", "types": ["TASK"],
               "verdict": "redact", "match": ["secret"],
               "replacement": "[x]"}])
    reply = await Axon(neuron_id="n", neuron_fn=asks, glia=c).handle_task(
        one_task(q="secret"),
    )
    assert reply.type is SignalType.CLARIFICATION
    assert reply.meta[META_KEY]["verdict"] == "redact"


# ---------------------------------------------------------------------------
# Axon, its own calls out and their replies in
# ---------------------------------------------------------------------------


async def test_an_outbound_tool_call_deny_raises_inside_the_neuron():
    got = {}

    async def neuron(inp, ctx, *, call_tool):
        try:
            await call_tool("fx", tool="shell", args={"cmd": "rm -rf /"})
        except PolicyRefusal as exc:
            got["reason"] = exc.reason
            got["scope"] = exc.outcome.scope
        return {"text": "handled"}

    card = load([{"id": "no-rm", "direction": "outbound",
                  "types": ["TOOL_CALL"], "verdict": "deny",
                  "tools": ["shell"], "match": [r"rm\s+-rf"],
                  "reason": "no recursive delete"}])
    calls: list = []
    b = Brain("g1")
    b.d.attach_effector(shell_effector(calls=calls))
    ax = Axon(neuron_id="n", neuron_fn=neuron, effectors=[FX],
              tool_standard="codex", glia=card)
    b.d.attach_axon(ax)
    await b.start()
    reply = await ax.handle_task(one_task())
    await b.stop()
    assert got == {"reason": "no recursive delete",
                   "scope": "outbound:TOOL_CALL"}
    assert reply.payload["output"] == {"text": "handled"}
    assert calls == [], "the call never reached the Effector"
    assert b.of(SignalType.TOOL_CALL) == [], "nothing refused crosses the bus"


async def test_an_unhandled_refusal_is_answered_by_the_axon():
    async def neuron(inp, ctx, *, call_tool):
        await call_tool("fx", tool="shell", args={"cmd": "rm -rf /"})
        return {"text": "not reached"}

    card = load([{"id": "no-rm", "direction": "outbound",
                  "types": ["TOOL_CALL"], "verdict": "deny",
                  "match": [r"rm\s+-rf"], "reason": "no recursive delete"}])
    b = Brain("g2")
    b.d.attach_effector(shell_effector())
    ax = Axon(neuron_id="n", neuron_fn=neuron, effectors=[FX],
              tool_standard="codex", glia=card)
    b.d.attach_axon(ax)
    await b.start()
    reply = await ax.handle_task(one_task())
    await b.stop()
    assert reply.type is SignalType.AGENT_OUTPUT, "never an ERROR"
    assert reply.payload["output"]["error"] == "no recursive delete"
    assert reply.payload["output"]["refused_by"] == "policy"


def _tool_neuron(cmd="rm -rf /"):
    async def neuron(inp, ctx):
        return {"response": json.dumps(
            {"name": "shell", "arguments": {"cmd": inp.get("cmd", cmd)}}
        )}
    return neuron


async def test_a_native_tool_call_refusal_uses_the_validate_args_shape():
    card = load([{"id": "no-rm", "direction": "outbound",
                  "types": ["TOOL_CALL"], "verdict": "deny",
                  "match": [r"rm\s+-rf"],
                  "reason": "recursive delete is not permitted"}])
    b = Brain("g3")
    b.d.attach_effector(shell_effector())
    ax = Axon(neuron_id="n", neuron_fn=_tool_neuron(), tool_standard="codex",
              effectors=[FX], glia=card)
    b.d.attach_axon(ax)
    await b.start()
    reply = await ax.handle_task(one_task())
    await b.stop()
    out = reply.payload["output"]
    # The same key a rejected argument uses, on the same channel.
    assert out["error"] == "recursive delete is not permitted"
    assert out["refused_by"] == "policy"
    assert out["tool"] == "shell"
    assert b.of(SignalType.TOOL_CALL) == []


async def test_a_native_tool_call_escalation_replaces_the_reply():
    c = Glia(card_id="c", mode="enforce")

    @c.on_signal(direction="outbound", types=["TOOL_CALL"])
    def h(sig):
        return Verdict.escalate("approve this command")

    b = Brain("g4")
    b.d.attach_effector(shell_effector())
    ax = Axon(neuron_id="n", neuron_fn=_tool_neuron(), tool_standard="codex",
              effectors=[FX], glia=c)
    b.d.attach_axon(ax)
    await b.start()
    reply = await ax.handle_task(one_task())
    await b.stop()
    assert reply.type is SignalType.PERMISSION
    assert "shell" in reply.payload["action"]


async def test_an_outbound_tool_call_is_redacted_before_it_leaves():
    calls: list = []
    card = load([{"id": "mask", "direction": "outbound",
                  "types": ["TOOL_CALL"], "verdict": "redact",
                  "match": ["/etc/shadow"], "replacement": "/dev/null"}])
    b = Brain("g5")
    b.d.attach_effector(shell_effector(calls=calls))
    ax = Axon(neuron_id="n", neuron_fn=_tool_neuron("cat /etc/shadow"),
              tool_standard="codex", effectors=[FX], glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert calls == [{"cmd": "cat /dev/null"}]
    [sent] = b.of(SignalType.TOOL_CALL)
    assert sent.payload["args"] == {"cmd": "cat /dev/null"}
    assert sent.meta[META_KEY]["verdict"] == "redact"


async def test_an_inbound_tool_result_is_redacted_before_the_neuron_reads_it():
    got = {}

    async def neuron(inp, ctx, *, call_tool):
        outcome = await call_tool("fx", tool="shell", args={"cmd": "whoami"})
        got["result"] = outcome.result
        return {"text": "ok"}

    card = load([{"id": "tokens", "direction": "inbound",
                  "types": ["TOOL_RESULT"], "verdict": "redact",
                  "match": [r"tok_[a-z0-9]+"], "replacement": "[token]"}])
    b = Brain("g6")
    b.d.attach_effector(shell_effector(results=[{"out": "root tok_abc123"}]))
    ax = Axon(neuron_id="n", neuron_fn=neuron, effectors=[FX],
              tool_standard="codex", glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert got["result"] == {"out": "root [token]"}
    [published] = b.of(SignalType.TOOL_RESULT)
    assert published.payload["result"] == {"out": "root tok_abc123"}, (
        "the Axon's gate rewrites its own copy, not the bus"
    )


async def test_an_inbound_tool_result_resend_repeats_the_call():
    calls: list = []
    got = {}

    async def neuron(inp, ctx, *, call_tool):
        outcome = await call_tool("fx", tool="shell", args={"cmd": "status"})
        got["result"] = outcome.result
        return {"text": "ok"}

    card = load([{"id": "no-stale", "direction": "inbound",
                  "types": ["TOOL_RESULT"], "verdict": "deny",
                  "match": ["stale"], "retry": "resend", "max_attempts": 2}])
    b = Brain("g7")
    b.d.attach_effector(shell_effector(
        calls=calls, results=[{"s": "stale"}, {"s": "fresh"}],
    ))
    ax = Axon(neuron_id="n", neuron_fn=neuron, effectors=[FX],
              tool_standard="codex", glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert len(calls) == 2, "the call was sent again once"
    assert got["result"] == {"s": "fresh"}
    assert len(b.of(SignalType.TOOL_CALL)) == 2
    assert len({s.id for s in b.of(SignalType.TOOL_CALL)}) == 2, (
        "a resend is a new signal, correlated on its own id"
    )


async def test_an_inbound_tool_result_resend_is_bounded():
    calls: list = []

    async def neuron(inp, ctx, *, call_tool):
        await call_tool("fx", tool="shell", args={"cmd": "status"})
        return {"text": "not reached"}

    card = load([{"id": "no-stale", "direction": "inbound",
                  "types": ["TOOL_RESULT"], "verdict": "deny",
                  "always": True, "reason": "never fresh",
                  "retry": "resend", "max_attempts": 2}])
    b = Brain("g8")
    b.d.attach_effector(shell_effector(calls=calls))
    ax = Axon(neuron_id="n", neuron_fn=neuron, effectors=[FX],
              tool_standard="codex", glia=card)
    b.d.attach_axon(ax)
    await b.start()
    reply = await ax.handle_task(one_task())
    await b.stop()
    assert len(calls) == 3, "the first call and two resends"
    assert reply.payload["output"]["error"] == "never fresh"


async def test_an_inbound_recalled_row_is_masked_before_the_neuron_reads_it():
    got = {}

    async def neuron(inp, ctx, *, recall):
        got["hits"] = [h.entry for h in await recall("mem", query={"q": 1})]
        return {"text": "ok"}

    card = load([{"id": "ssn", "direction": "inbound", "types": ["RECALLED"],
                  "verdict": "redact", "keys": ["ssn"]}])
    b = Brain("g9")
    b.d.attach_engram(memory_engram())
    ax = Axon(neuron_id="n", neuron_fn=neuron, engrams=[EG], glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert got["hits"] == [{"name": "ada"}]


async def test_an_outbound_imprint_deny_means_nothing_is_written():
    got = {}

    async def neuron(inp, ctx, *, imprint):
        try:
            await imprint("mem", op="add", entry={"password": "hunter2"})
        except PolicyRefusal as exc:
            got["scope"] = exc.outcome.scope
        return {"text": "ok"}

    card = load([{"id": "no-creds", "direction": "outbound",
                  "types": ["IMPRINT"], "verdict": "deny",
                  "match": ["password"], "reason": "no credentials"}])
    b = Brain("g10")
    b.d.attach_engram(memory_engram())
    ax = Axon(neuron_id="n", neuron_fn=neuron, engrams=[EG], glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert got["scope"] == "outbound:IMPRINT"
    assert b.of(SignalType.IMPRINT) == []
    assert b.of(SignalType.IMPRINTED) == []


async def test_the_axon_reads_the_imprinted_that_comes_back():
    seen = []
    card = Glia(card_id="c", mode="enforce")

    @card.on_signal(direction="inbound", types=["IMPRINTED"])
    def watch(sig):
        seen.append(sig.payload.get("id"))

    async def neuron(inp, ctx, *, imprint):
        await imprint("mem", op="add", entry={"a": 1}, await_ack=True)
        return {"text": "ok"}

    b = Brain("g11")
    b.d.attach_engram(memory_engram())
    ax = Axon(neuron_id="n", neuron_fn=neuron, engrams=[EG], glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert seen == ["x1"]


async def test_calls_through_axon_dendrite_are_gated_too():
    got = {}
    card = load([{"id": "no-recall", "direction": "outbound",
                  "types": ["RECALL"], "verdict": "deny", "always": True,
                  "reason": "no memory reads from this Neuron"}])
    b = Brain("g12")
    b.d.attach_engram(memory_engram())

    async def neuron(inp, ctx):
        try:
            await ax.dendrite.recall(engram_id="eg1", query={"q": 1},
                                     deadline_ms=500)
        except PolicyRefusal as exc:
            got["reason"] = exc.reason
        return {"text": "ok"}

    ax = Axon(neuron_id="n", neuron_fn=neuron, glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert got["reason"] == "no memory reads from this Neuron"
    assert b.of(SignalType.RECALL) == []


async def test_a_denied_responder_is_dropped_from_a_merged_recall():
    got = {}
    card = Glia(card_id="c", mode="enforce")

    @card.on_signal(direction="inbound", types=["RECALLED"])
    def only_eg1(sig):
        if sig.directed_id != "eg1":
            return Verdict.deny("unknown responder")
        return None

    async def neuron(inp, ctx, *, recall):
        res = await recall("mem", query={"q": 1}, recall_mode="merge",
                           deadline_ms=200)
        got["ids"] = sorted(res.engram_ids)
        got["hits"] = [h.entry for h in res]
        return {"text": "ok"}

    b = Brain("g13")
    for eid in ("eg1", "eg2"):
        eg = Engram.serve(engram_id=eid, engram_kind="mem")

        @eg.on_recall
        async def read(query, _eid=eid):
            return [Hit(id=_eid, entry={"from": _eid})]

        b.d.attach_engram(eg)
    binding = EngramBinding(name="mem", directed_type="mem",
                            default_deadline_ms=200)
    ax = Axon(neuron_id="n", neuron_fn=neuron, engrams=[binding], glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert got["ids"] == ["eg1"]
    assert got["hits"] == [{"from": "eg1"}]


async def test_a_backend_call_is_not_gated_as_the_calling_axon():
    """On an in-process synapse a TOOL_CALL is serviced inside the calling
    Axon's own publish. The Effector's work is not the Axon's call."""
    got = {}
    card = load([{"id": "no-recall", "direction": "outbound",
                  "types": ["RECALL"], "verdict": "deny", "always": True}])
    b = Brain("g14")
    b.d.attach_engram(memory_engram())
    fx = Effector.serve(effector_id="fx1", effector_kind="shell")

    @fx.on_tool_call
    async def run(tool, args):
        res = await b.d.recall(engram_id="eg1", query={"q": 1},
                               deadline_ms=500)
        got["n"] = len(res)
        return {"ok": True}

    b.d.attach_effector(fx)

    async def neuron(inp, ctx, *, call_tool):
        await call_tool("fx", tool="shell", args={})
        return {"text": "ok"}

    ax = Axon(neuron_id="n", neuron_fn=neuron, effectors=[FX],
              tool_standard="codex", glia=card)
    b.d.attach_axon(ax)
    await b.start()
    await ax.handle_task(one_task())
    await b.stop()
    assert got["n"] == 1


# ---------------------------------------------------------------------------
# Effector, both ways
# ---------------------------------------------------------------------------


async def test_an_effector_inbound_deny_answers_tool_result_never_error():
    card = load([{"id": "no-rm", "direction": "inbound",
                  "types": ["TOOL_CALL"], "verdict": "deny",
                  "tools": ["shell"], "match": [r"rm\s+-rf"],
                  "reason": "no recursive delete"}])
    calls: list = []
    b = Brain("e1")
    b.d.attach_effector(shell_effector(card, calls=calls))
    await b.start(SignalType.TOOL_RESULT, SignalType.ERROR)
    await b.syn.publish("cosmonapse.e1.TOOL_CALL", cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(), tool="shell",
        args={"cmd": "rm -rf /"}, directed=Directed(id="fx1"),
    ))
    await asyncio.sleep(0.05)
    await b.stop()
    [result] = b.of(SignalType.TOOL_RESULT)
    assert result.payload["error"] == "no recursive delete"
    assert result.meta[META_KEY]["component"] == "fx1"
    assert calls == []
    assert not b.of(SignalType.ERROR), (
        "a policy verdict must never produce an ERROR signal"
    )


async def test_an_effector_outbound_result_is_redacted_before_publish():
    card = load([{"id": "tokens", "direction": "outbound",
                  "types": ["TOOL_RESULT"], "verdict": "redact",
                  "match": [r"tok_[a-z0-9]+"], "replacement": "[token]"}])
    b = Brain("e2")
    b.d.attach_effector(shell_effector(card, results=[{"out": "tok_abc"}]))
    await b.start(SignalType.TOOL_RESULT)
    await b.syn.publish("cosmonapse.e2.TOOL_CALL", cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(), tool="shell",
        args={}, directed=Directed(id="fx1"),
    ))
    await asyncio.sleep(0.05)
    await b.stop()
    [result] = b.of(SignalType.TOOL_RESULT)
    assert result.payload["result"] == {"out": "[token]"}


async def test_an_effector_outbound_deny_can_resend_by_re_invoking():
    calls: list = []
    card = load([{"id": "no-empty", "direction": "outbound",
                  "types": ["TOOL_RESULT"], "verdict": "deny",
                  "match": ['"rows": \\[\\]'], "retry": "resend",
                  "max_attempts": 2, "reason": "empty result"}])
    b = Brain("e3")
    b.d.attach_effector(shell_effector(
        card, calls=calls, results=[{"rows": []}, {"rows": [1]}],
    ))
    await b.start(SignalType.TOOL_RESULT)
    await b.syn.publish("cosmonapse.e3.TOOL_CALL", cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(), tool="shell",
        args={}, directed=Directed(id="fx1"),
    ))
    await asyncio.sleep(0.05)
    await b.stop()
    assert len(calls) == 2
    [result] = b.of(SignalType.TOOL_RESULT)
    assert result.payload["result"] == {"rows": [1]}
    assert result.meta[META_KEY]["attempt"] == 2


async def test_an_effector_outbound_resend_that_never_passes_refuses():
    calls: list = []
    card = load([{"id": "no-empty", "direction": "outbound",
                  "types": ["TOOL_RESULT"], "verdict": "deny",
                  "always": True, "retry": "resend", "max_attempts": 1,
                  "reason": "never good enough"}])
    b = Brain("e4")
    b.d.attach_effector(shell_effector(card, calls=calls))
    await b.start(SignalType.TOOL_RESULT)
    await b.syn.publish("cosmonapse.e4.TOOL_CALL", cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(), tool="shell",
        args={}, directed=Directed(id="fx1"),
    ))
    await asyncio.sleep(0.05)
    await b.stop()
    assert len(calls) == 2
    [result] = b.of(SignalType.TOOL_RESULT)
    assert result.payload["error"] == "never good enough"
    assert "result" not in result.payload


# ---------------------------------------------------------------------------
# Engram, both ways
# ---------------------------------------------------------------------------


async def test_an_engram_inbound_imprint_deny_answers_imprinted_with_error():
    card = load([{"id": "no-creds", "direction": "inbound",
                  "types": ["IMPRINT"], "verdict": "deny", "ops": ["add"],
                  "match": ["password"],
                  "reason": "no credentials in memory"}])
    b = Brain("m1")
    b.d.attach_engram(memory_engram(card))
    await b.start(SignalType.IMPRINTED, SignalType.ERROR)
    await b.syn.publish("cosmonapse.m1.IMPRINT", cn.imprint_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(), op="add",
        entry={"password": "hunter2"}, directed=Directed(id="eg1"),
    ))
    await asyncio.sleep(0.05)
    await b.stop()
    [ack] = b.of(SignalType.IMPRINTED)
    assert ack.payload["error"] == "no credentials in memory"
    assert not b.of(SignalType.ERROR)


async def test_an_engram_outbound_recalled_is_masked_before_publish():
    card = load([{"id": "ssn", "direction": "outbound", "types": ["RECALLED"],
                  "verdict": "redact", "keys": ["ssn"],
                  "reason": "ssn never leaves the engram"}])
    b = Brain("m2")
    b.d.attach_engram(memory_engram(card))
    await b.start(SignalType.RECALLED)
    await b.syn.publish("cosmonapse.m2.RECALL", cn.recall_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        query={"q": "ada"}, directed=Directed(id="eg1"),
    ))
    await asyncio.sleep(0.05)
    await b.stop()
    [out] = b.of(SignalType.RECALLED)
    assert [h["entry"] for h in out.payload["hits"]] == [{"name": "ada"}]
    assert out.meta[META_KEY]["verdict"] == "redact"
    assert out.meta[META_KEY]["direction"] == "outbound"


async def test_an_engram_reads_the_imprinted_it_sends():
    seen = []
    card = Glia(card_id="c", mode="enforce")

    @card.on_signal(direction="outbound")
    def watch(sig):
        seen.append(sig.type)

    b = Brain("m3")
    b.d.attach_engram(memory_engram(card))
    await b.start(SignalType.IMPRINTED)
    await b.syn.publish("cosmonapse.m3.IMPRINT", cn.imprint_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(), op="add",
        entry={"a": 1}, directed=Directed(id="eg1"),
    ))
    await asyncio.sleep(0.05)
    await b.stop()
    assert seen == [SignalType.IMPRINTED]


# ---------------------------------------------------------------------------
# Mounting, announcing, and the Dendrite's non-role
# ---------------------------------------------------------------------------


async def test_the_card_can_be_mounted_at_attach_time():
    card = load([{"id": "r", "verdict": "deny", "always": True}])
    fx = shell_effector()
    d = Dendrite(synapse=MemorySynapse(), namespace="p4")
    d.attach_effector(fx, glia=card)
    assert fx.glia is card


async def test_register_announces_the_card_and_its_policy_version():
    card = load([{"id": "r", "verdict": "deny", "always": True}])
    b = Brain("p5")
    b.d.attach_effector(shell_effector(card))
    b.d.attach_engram(memory_engram(card))
    await b.syn.connect()

    async def cap(sig):
        b.seen.append(sig)

    await b.syn.subscribe("cosmonapse.p5.REGISTER", cap)
    await b.d.start()
    await asyncio.sleep(0.05)
    await b.stop()
    announced = [s for s in b.seen if META_KEY in s.meta]
    assert announced, "REGISTER must carry the card and policy version"
    rec = announced[0].meta[META_KEY]
    assert rec["policy_version"] == "7"
    assert rec["card_id"] == "c"
    assert "issued_at" in rec, "card age is the revocation telemetry"


def test_a_dendrite_never_evaluates_policies_and_says_so(caplog):
    card = load([{"id": "r", "verdict": "deny", "always": True}])
    with caplog.at_level(logging.WARNING, logger="cosmonapse.dendrite"):
        d = Dendrite(synapse=MemorySynapse(), namespace="p6", glia=card)
    assert d.glia is card
    assert "never evaluates" in caplog.text


# ---------------------------------------------------------------------------
# Audit mode blocks nothing, anywhere
# ---------------------------------------------------------------------------


async def test_audit_blocks_nothing_either_way():
    card = load(
        [{"id": "all", "verdict": "deny", "always": True,
          "reason": "would refuse"}],
        mode="audit",
    )
    ax = Axon(neuron_id="n", neuron_fn=echo, glia=card)
    reply = await ax.handle_task(one_task(q="x"))
    assert reply.payload["output"] == {"text": "echo:x"}
    assert reply.meta[META_KEY]["verdict"] == "would_deny"

    eg = memory_engram(card)
    out = await eg.handle(cn.recall_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        query={"q": "ada"}, directed=Directed(id="eg1"),
    ))
    assert [h["entry"] for h in out.payload["hits"]] == [
        {"ssn": "123-45", "name": "ada"},
    ]
    assert out.meta[META_KEY]["verdict"] == "would_deny"


@pytest.mark.parametrize("mode", ["off"])
async def test_mode_off_reads_nothing_anywhere(mode):
    card = load([{"id": "all", "verdict": "deny", "always": True}], mode=mode)
    reply = await Axon(neuron_id="n", neuron_fn=echo, glia=card).handle_task(
        one_task(q="x"),
    )
    assert reply.payload["output"] == {"text": "echo:x"}
    assert reply.meta == {}
    assert card.counters["checked"] == 0
