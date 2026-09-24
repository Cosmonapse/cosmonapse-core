"""The repair loop, and the trace limits the Dendrite owns.

One loop, three callers: an unclaimed output, a tool call whose
arguments failed validation, and a policy refusal whose policy asks to
``reask``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import cosmonapse as cn
from cosmonapse import (
    Axon,
    Dendrite,
    Directed,
    Glia,
    MemorySynapse,
    RepairPolicy,
    SignalType,
    TraceLimitExceeded,
    task_signal,
)
from cosmonapse.glia import META_KEY


def one_task(neuron_id="n", trace_id=None, **payload):
    if trace_id is None:
        return task_signal(directed=Directed(id=neuron_id), input=payload)
    return cn.Signal(
        type=SignalType.TASK, trace_id=trace_id,
        directed=Directed(id=neuron_id), payload={"input": payload},
    )


def deny_card(reason="no keys in output", **over):
    body = {
        "card_id": "c", "version": "1", "mode": "enforce",
        "repair": {"max_attempts": 2},
        "policies": [{
            "id": "no-keys", "direction": "outbound",
            "types": ["AGENT_OUTPUT"], "verdict": "deny", "retry": "reask",
            "match": [r"sk-[A-Za-z0-9]{6,}"], "reason": reason,
        }],
    }
    body.update(over)
    return Glia.load(json.dumps(body))


# ---------------------------------------------------------------------------
# No loop unless something asked for one
# ---------------------------------------------------------------------------


async def test_no_card_and_no_repair_means_no_loop():
    calls = []

    async def neuron(inp, ctx):
        calls.append(1)
        return {"text": "whatever"}

    ax = Axon(neuron_id="n", neuron_fn=neuron)
    reply = await ax.handle_task(one_task())
    assert calls == [1]
    assert reply.meta == {}


async def test_strict_output_is_silent_when_nothing_could_have_claimed():
    calls = []

    async def neuron(inp, ctx):
        calls.append(1)
        return {"text": "a perfectly good plain answer"}

    # No recognisers, no parser, no tool dialect: strict has no opinion.
    ax = Axon(
        neuron_id="n", neuron_fn=neuron, strict_output=True,
        repair=RepairPolicy(max_attempts=2),
    )
    reply = await ax.handle_task(one_task())
    assert calls == [1]
    assert reply.payload["output"] == {"text": "a perfectly good plain answer"}


# ---------------------------------------------------------------------------
# Correction repair
# ---------------------------------------------------------------------------


async def test_a_refusal_is_re_asked_and_the_correction_reaches_the_neuron():
    calls = []

    async def neuron(inp, ctx):
        calls.append(inp.get("repair"))
        if inp.get("repair"):
            return {"text": "clean answer"}
        return {"text": "the key is sk-SECRET123456"}

    ax = Axon(neuron_id="n", neuron_fn=neuron, glia=deny_card())
    reply = await ax.handle_task(one_task())
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1]["kind"] == "GUARD_RETRY"
    assert calls[1]["problem"] == "no keys in output"
    assert reply.payload["output"] == {"text": "clean answer"}
    attempts = reply.meta[META_KEY]["attempts"]
    assert [a["outcome"] for a in attempts] == ["re_asked"]
    assert [a["kind"] for a in attempts] == ["GUARD_RETRY"]
    assert [a["domain"] for a in attempts] == ["security"]


async def test_an_exhausted_loop_returns_the_refusal_not_an_error():
    calls = []

    async def stubborn(inp, ctx):
        calls.append(1)
        return {"text": "sk-ALWAYSBAD1234"}

    ax = Axon(neuron_id="n", neuron_fn=stubborn, glia=deny_card())
    reply = await ax.handle_task(one_task())
    assert len(calls) == 3, "the first pass plus max_attempts corrections"
    assert reply.type is SignalType.AGENT_OUTPUT, (
        "an exhausted refusal is still a refusal, never an ERROR"
    )
    assert reply.payload["output"]["refused_by"] == "policy"
    outcomes = [a["outcome"] for a in reply.meta[META_KEY]["attempts"]]
    assert outcomes == ["re_asked", "re_asked", "exhausted"]


async def test_a_trace_past_the_limit_gets_the_one_legitimate_error():
    async def stubborn(inp, ctx):
        return {"text": "sk-ALWAYSBAD1234"}

    ax = Axon(neuron_id="n", neuron_fn=stubborn, glia=deny_card())
    first = await ax.handle_task(one_task())
    # A second TASK on the SAME trace: the loop is broken, not the request.
    second = await ax.handle_task(one_task(trace_id=first.trace_id))
    assert second.type is SignalType.ERROR
    assert second.payload["code"] == "REPAIR_EXHAUSTED"
    assert second.payload["recoverable"] is False, (
        "not recoverable, so default_retry_on will not re-dispatch it"
    )
    assert second.meta[META_KEY]["attempts"], "the ERROR carries the list"


async def test_a_fresh_trace_starts_over():
    async def stubborn(inp, ctx):
        return {"text": "sk-ALWAYSBAD1234"}

    ax = Axon(neuron_id="n", neuron_fn=stubborn, glia=deny_card())
    await ax.handle_task(one_task())
    again = await ax.handle_task(one_task())
    assert again.type is SignalType.AGENT_OUTPUT


async def test_a_builder_that_changes_nothing_stops_the_loop_at_once():
    calls = []

    async def stubborn(inp, ctx):
        calls.append(1)
        return {"text": "sk-ALWAYSBAD1234"}

    ax = Axon(neuron_id="n", neuron_fn=stubborn, glia=deny_card())

    @ax.repairs
    def useless(inp, problem):
        return dict(inp)  # identical, so the next attempt cannot differ

    reply = await ax.handle_task(one_task())
    assert len(calls) == 1, "no model call is spent on a request that cannot differ"
    assert reply.meta[META_KEY]["attempts"][0]["outcome"] == "uncorrectable"


async def test_a_repairs_hook_owns_the_next_input():
    seen = []

    async def neuron(inp, ctx):
        seen.append(dict(inp))
        if inp.get("q", "").startswith("PLEASE"):
            return {"text": "fine"}
        return {"text": "sk-SECRET123456"}

    ax = Axon(neuron_id="n", neuron_fn=neuron, glia=deny_card())

    @ax.repairs
    async def rebuild(inp, problem, *, attempt, kind):
        return {"q": f"PLEASE avoid: {problem} (attempt {attempt}, {kind})"}

    reply = await ax.handle_task(one_task(q="go"))
    assert reply.payload["output"] == {"text": "fine"}
    assert "PLEASE avoid: no keys in output" in seen[1]["q"]


async def test_a_raising_builder_stops_the_loop_rather_than_the_task():
    async def stubborn(inp, ctx):
        return {"text": "sk-ALWAYSBAD1234"}

    ax = Axon(neuron_id="n", neuron_fn=stubborn, glia=deny_card())

    @ax.repairs
    def broken(inp, problem):
        raise RuntimeError("builder bug")

    reply = await ax.handle_task(one_task())
    assert reply.type is SignalType.AGENT_OUTPUT
    assert reply.payload["output"]["refused_by"] == "policy"


# ---------------------------------------------------------------------------
# Unclaimed output
# ---------------------------------------------------------------------------


async def test_an_unclaimed_output_is_repaired_rather_than_shipped():
    calls = []

    async def neuron(inp, ctx):
        calls.append(1)
        return {"text": "looks like a tool call but parses to nothing"}

    ax = Axon(
        neuron_id="n", neuron_fn=neuron, strict_output=True,
        repair=RepairPolicy(max_attempts=1),
    )

    @ax.detects_output
    def claims_nothing(raw):
        return None

    reply = await ax.handle_task(one_task())
    assert len(calls) == 2, "the miss costs one more round of prompting"
    kinds = [a["kind"] for a in reply.meta[META_KEY]["attempts"]]
    assert kinds == ["EVAL_RETRY", "EVAL_RETRY"]
    assert {a["domain"] for a in reply.meta[META_KEY]["attempts"]} == {"eval"}
    # And the fallback is exactly what the Axon returned before strict mode.
    assert reply.payload["output"] == {
        "text": "looks like a tool call but parses to nothing"
    }


async def test_a_claimed_output_never_triggers_the_loop():
    calls = []

    async def neuron(inp, ctx):
        calls.append(1)
        return {"text": "hello"}

    ax = Axon(
        neuron_id="n", neuron_fn=neuron, strict_output=True,
        repair=RepairPolicy(max_attempts=2),
    )

    @ax.detects_output
    def claims(raw):
        return {"answer": raw["text"]}

    reply = await ax.handle_task(one_task())
    assert calls == [1]
    assert reply.payload["output"] == {"answer": "hello"}


# ---------------------------------------------------------------------------
# Invalid tool arguments
# ---------------------------------------------------------------------------


async def test_invalid_tool_arguments_feed_the_same_loop():
    calls = []

    async def neuron(inp, ctx):
        calls.append(1)
        return {"response": json.dumps(
            {"name": "read", "arguments": {"wrong": 1}}
        )}

    def read(path: str) -> str:
        """Read a file."""
        return ""

    schema = cn.tool_schema(read, params={"path": "path from the root"})
    binding = cn.EffectorBinding(
        name="fx", directed_id="fx1", tools=("read",), schemas=(schema,),
    )
    ax = Axon(
        neuron_id="n", neuron_fn=neuron, tool_standard="codex",
        effectors=[binding], repair=RepairPolicy(max_attempts=1),
    )
    reply = await ax.handle_task(one_task())
    assert len(calls) == 2
    kinds = [a["kind"] for a in reply.meta[META_KEY]["attempts"]]
    assert kinds == ["TOOL_RETRY", "TOOL_RETRY"]
    assert {a["domain"] for a in reply.meta[META_KEY]["attempts"]} == {"tool"}
    assert "error" in reply.payload["output"]


# ---------------------------------------------------------------------------
# The two retry flavours must not share a knob
# ---------------------------------------------------------------------------


async def test_a_policy_refusal_never_feeds_transient_retry():
    from cosmonapse.glia import run_with_transient_retry

    tries = []

    async def call():
        tries.append(1)
        raise RuntimeError("a dropped connection")

    with pytest.raises(RuntimeError):
        await run_with_transient_retry(
            call,
            RepairPolicy(transient_attempts=2, transient_backoff_s=0.0),
            label="test",
        )
    assert len(tries) == 3

    # And a refusal is raised OUTSIDE that call, so it can never be
    # mistaken for a transient fault. Proven by the Effector path: a
    # denied call runs invoke() zero times.
    from cosmonapse import Effector

    card = Glia.load(json.dumps({
        "card_id": "c", "mode": "enforce",
        "repair": {"transient_attempts": 5},
        "policies": [{"id": "no", "direction": "inbound",
                      "types": ["TOOL_CALL"], "verdict": "deny",
                      "always": True, "reason": "closed"}],
    }))
    ran = []
    fx = Effector.serve(effector_id="fx1", effector_kind="k", glia=card)

    @fx.on_tool_call
    async def run(tool, args):
        ran.append(1)
        return {"ok": True}

    reply = await fx.handle(cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        tool="anything", args={},
    ))
    assert reply.payload["error"] == "closed"
    assert ran == [], "N guaranteed denials is not a retry strategy"


async def test_a_transient_fault_recovers_and_rides_the_record():
    from cosmonapse import Effector, ToolOutcome

    # A BACKEND fault, which is the only thing transient retry is for: a
    # served Effector turns a handler raise into error on the outcome, so
    # only a backend that actually raises from invoke() reaches here.
    card = Glia.load(json.dumps({
        "card_id": "c", "mode": "enforce",
        "repair": {"transient_attempts": 2, "transient_backoff_s": 0.0},
    }))

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
    fx._glia = card
    reply = await fx.handle(cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        tool="go", args={},
    ))
    assert reply.payload["result"] == {"ok": True}
    assert Flaky.tries == 2
    record = reply.meta[META_KEY]
    assert record["attempts"][-1]["outcome"] == "recovered"
    assert record["attempts"][0]["error"] == "ConnectionResetError"


# ---------------------------------------------------------------------------
# STOP mid-repair
# ---------------------------------------------------------------------------


async def test_the_stopped_ack_carries_the_attempts_so_far():
    async def slow_and_stubborn(inp, ctx):
        await asyncio.sleep(0.01)
        return {"text": "sk-ALWAYSBAD1234"}

    ax = Axon(neuron_id="n", neuron_fn=slow_and_stubborn, glia=deny_card())
    syn = MemorySynapse()
    d = Dendrite(synapse=syn, namespace="r2")
    d.attach_axon(ax)
    await d.start()
    seen: list = []

    async def cap(sig):
        seen.append(sig)

    await syn.subscribe("cosmonapse.r2.STOPPED", cap)
    task = one_task()
    child = asyncio.ensure_future(ax.handle_task(task))
    d._register_trace_task(task.trace_id, child)
    await asyncio.sleep(0.03)
    await d._on_stop(cn.stop_signal(trace_id=task.trace_id))
    await asyncio.sleep(0.02)
    await d.stop()

    acks = [s for s in seen if s.type is SignalType.STOPPED]
    assert acks, "a Dendrite with a stake in the trace acks"
    assert acks[0].meta[META_KEY]["attempts"], (
        "otherwise the case most worth auditing emits nothing"
    )
    assert ax.repair_attempts(task.trace_id) == [], "the trace is forgotten"


# ---------------------------------------------------------------------------
# The deadline-nesting warning
# ---------------------------------------------------------------------------


async def test_repair_with_tools_warns_about_the_nested_deadline(caplog):
    def read(path: str) -> str:
        """Read a file."""
        return ""

    schema = cn.tool_schema(read)
    binding = cn.EffectorBinding(
        name="fx", directed_id="fx1", tools=("read",), schemas=(schema,),
    )
    with caplog.at_level("WARNING"):
        Axon(
            neuron_id="n", neuron_fn=lambda i, c: None,
            tool_standard="codex", effectors=[binding],
            repair=RepairPolicy(max_attempts=2),
        )
    assert any("mid-repair" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Trace limits
# ---------------------------------------------------------------------------


def limits_card(**limits):
    return Glia.load(json.dumps({
        "card_id": "d", "mode": "enforce", "limits": limits,
    }))


async def test_component_mode_is_exact_and_needs_no_subscription():
    card = limits_card(mode="component", max_tool_calls=2)
    syn = MemorySynapse()
    d = Dendrite(synapse=syn, namespace="l1", glia=card)
    await d.start()
    trace, parent = cn.new_trace_id(), cn.new_event_id()
    allowed = 0
    for _ in range(6):
        try:
            await d.emit_tool_call(
                trace_id=trace, parent_id=parent, tool="x", args={},
            )
        except TraceLimitExceeded:
            break
        allowed += 1
    assert allowed == 2
    assert d.trace_counter.snapshot(trace)["tool_calls"] == 2
    # The other half of the accept criterion, which this test's name
    # claims and used to leave unchecked: component mode adds NO
    # subscription. AGENT_OUTPUT is a PATHWAY_TYPES member that start()
    # does not subscribe to on its own, so it is the discriminator.
    assert SignalType.AGENT_OUTPUT not in d._inbound_subs
    await d.stop()


async def test_a_reply_still_gets_out_when_the_trace_is_over_its_limit():
    card = limits_card(mode="component", max_actions=1)
    syn = MemorySynapse()
    d = Dendrite(synapse=syn, namespace="l2", glia=card)
    await d.start()
    trace, parent = cn.new_trace_id(), cn.new_event_id()
    await d.emit_plan(trace_id=trace, parent_id=parent, steps=[{"do": "x"}])
    with pytest.raises(TraceLimitExceeded):
        await d.emit_plan(trace_id=trace, parent_id=parent, steps=[{"do": "y"}])
    # A refusal that could not be published would make the Dendrite go
    # silent and every caller time out instead of being told.
    await d._publish(cn.agent_output_signal(
        trace_id=trace, parent_id=parent, output={"ok": True},
    ))
    await d.stop()


async def test_no_card_means_no_counter_at_all():
    d = Dendrite(synapse=MemorySynapse(), namespace="l3")
    assert d.glia is None
    assert d.trace_counter is None


async def test_on_exceed_stop_trace_asks_the_workflow_to_stop_once():
    card = limits_card(mode="component", max_actions=1, on_exceed="stop_trace")
    syn = MemorySynapse()
    d = Dendrite(synapse=syn, namespace="l4", glia=card)
    await d.start()
    seen: list = []

    async def cap(sig):
        seen.append(sig)

    await syn.subscribe("cosmonapse.l4.STOP", cap)
    trace, parent = cn.new_trace_id(), cn.new_event_id()
    await d.emit_plan(trace_id=trace, parent_id=parent, steps=[])
    for _ in range(3):
        with pytest.raises(TraceLimitExceeded):
            await d.emit_plan(trace_id=trace, parent_id=parent, steps=[])
    await asyncio.sleep(0.05)
    await d.stop()
    stops = [s for s in seen if s.trace_id == trace]
    assert len(stops) == 1, "latched: asked once, not on every action"


async def test_trace_mode_bounds_a_burst_not_just_a_paced_loop():
    """Counting only what ARRIVES did not bound a burst at all.

    A Synapse delivers asynchronously, so ten emits with no await in
    between cleared every pre-check while the count was still zero: on a
    cap of three, all ten got through. Since a spinning component is
    exactly a tight loop, the mode failed hardest at the one runaway it
    exists for. Both modes now count on the way out, and the signal id
    keeps trace mode from counting its own loopback twice.
    """
    for paced in (True, False):
        card = limits_card(mode="trace", max_actions=3)
        d = Dendrite(synapse=MemorySynapse(), namespace="l5", glia=card)
        await d.start()
        trace, parent = cn.new_trace_id(), cn.new_event_id()
        allowed = 0
        for _ in range(10):
            try:
                await d.emit_plan(trace_id=trace, parent_id=parent, steps=[])
            except TraceLimitExceeded:
                break
            allowed += 1
            if paced:
                await asyncio.sleep(0)
        await asyncio.sleep(0.03)
        assert allowed == 3, f"paced={paced} let {allowed} through on a cap of 3"
        await d.stop()


async def test_trace_mode_still_counts_a_peers_actions():
    """Which is the whole reason the mode exists. Its own actions are now
    exact; a peer's are still approximate, soft by at most one action per
    concurrent actor, because two Dendrites can each check and act before
    either sees the other."""
    card = limits_card(mode="trace", max_actions=2)
    syn = MemorySynapse()
    d = Dendrite(synapse=syn, namespace="l6", glia=card)
    await d.start()
    peer = Dendrite(synapse=syn, namespace="l6", dendrite_id="peer")
    await peer.start()
    trace = cn.new_trace_id()
    for _ in range(2):
        await peer.emit_plan(
            trace_id=trace, parent_id=cn.new_event_id(), steps=[],
        )
    await asyncio.sleep(0.03)
    assert d.trace_counter.snapshot(trace)["actions"] == 2
    with pytest.raises(TraceLimitExceeded):
        await d.emit_plan(trace_id=trace, parent_id=cn.new_event_id(), steps=[])
    await peer.stop()
    await d.stop()
