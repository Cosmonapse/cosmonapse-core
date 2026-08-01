"""
Tests for the cognition decorator/emit surface and filter ergonomics.

Covers:
  * Cognition decorators (@on_plan, @on_critique, @on_memory_append, ...)
    receive matching signals  -  plugging the AXON_TYPES-only gap in
    Dendrite._dispatch_inbound.
  * emit_* helpers for cognition signals round-trip through the bus and
    produce well-formed envelopes that match the underlying builders.
  * Filter kwargs (neuron= / capability= / trace_id=) gate dispatch so
    handlers registered on the same SignalType don't all fire on every
    signal.
  * The bare @orch.on_x decorator form still works (back-compat).
"""

import asyncio

import pytest

from cosmonapse import (
    Axon,
    Dendrite,
    Directed,
    MemoryRegistryStore,
    MemorySynapse,
    Signal,
    SignalType,
    new_event_id,
    new_trace_id,
)


def _run(coro):
    return asyncio.run(coro)


async def _make_orch(*, registry: bool = False):
    synapse = MemorySynapse()
    await synapse.connect()
    store = MemoryRegistryStore() if registry else None
    orch = Dendrite(
        synapse=synapse, registry_store=store, namespace="cog",
        heartbeat_s=0,  # heartbeat noise is irrelevant to these tests
    )
    return synapse, orch


# ---------------------------------------------------------------------------
# Cognition decorators receive the right signals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decorator_name,signal_type,emit_method,emit_kwargs",
    [
        ("on_plan", SignalType.PLAN, "emit_plan",
         {"steps": [{"id": 1, "do": "search"}]}),
        ("on_thought_delta", SignalType.THOUGHT_DELTA, "emit_thought_delta",
         {"delta": "hello"}),
        ("on_tool_call", SignalType.TOOL_CALL, "emit_tool_call",
         {"tool": "search", "args": {"q": "x"}}),
        ("on_tool_result", SignalType.TOOL_RESULT, "emit_tool_result",
         {"tool": "search", "result": {"hits": 0}}),
        ("on_memory_append", SignalType.MEMORY_APPEND, "emit_memory_append",
         {"key": "k", "value": {"v": 1}}),
        ("on_critique", SignalType.CRITIQUE, "emit_critique",
         {"target_event_id": new_event_id(),
          "issues": [{"type": "factual"}], "verdict": "revise"}),
        ("on_escalation", SignalType.ESCALATION, "emit_escalation",
         {"reason": "blocked"}),
        ("on_consensus", SignalType.CONSENSUS, "emit_consensus",
         {"members": ["a", "b"], "verdict": "yes"}),
        ("on_context_sync", SignalType.CONTEXT_SYNC, "emit_context_sync",
         {"snapshot": {"version": 1}}),
    ],
)
def test_cognition_decorators_receive_matching_signals(
    decorator_name, signal_type, emit_method, emit_kwargs,
):
    async def run():
        synapse, orch = await _make_orch()
        seen: list[Signal] = []
        try:
            decorator = getattr(orch, decorator_name)

            @decorator
            async def handler(sig):
                seen.append(sig)

            await orch.start()

            trace = new_trace_id()
            parent = new_event_id()
            await getattr(orch, emit_method)(
                trace_id=trace, parent_id=parent, **emit_kwargs,
            )
            await asyncio.sleep(0.02)

            assert len(seen) == 1, (
                f"{decorator_name} handler did not receive a "
                f"{signal_type.value} signal"
            )
            assert seen[0].type is signal_type
            assert seen[0].trace_id == trace
            assert seen[0].parent_id == parent
        finally:
            await orch.stop()
            await synapse.close()

    _run(run())


# ---------------------------------------------------------------------------
# Bare and filter-call decorator forms both work
# ---------------------------------------------------------------------------


def test_bare_on_agent_output_still_works():
    async def run():
        synapse, orch = await _make_orch()
        seen = []
        try:
            @orch.on_agent_output
            async def h(sig):
                seen.append(sig)

            await orch.start()

            from cosmonapse import agent_output_signal
            sig = agent_output_signal(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                directed=Directed(id="n"), output={"r": 1},
            )
            await synapse.publish(f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}", sig)
            await asyncio.sleep(0.02)
            assert len(seen) == 1
        finally:
            await orch.stop()
            await synapse.close()
    _run(run())


def test_on_agent_output_filter_by_neuron():
    """Filter-call form: @on_agent_output(neuron='answerer') only fires
    for that neuron."""
    async def run():
        synapse, orch = await _make_orch()
        from cosmonapse import agent_output_signal

        for_answerer = []
        all_outputs = []
        try:
            @orch.on_agent_output(neuron="answerer")
            async def only_answerer(sig):
                for_answerer.append(sig)

            @orch.on_agent_output
            async def catch_all(sig):
                all_outputs.append(sig)

            await orch.start()

            for nid in ("answerer", "summarizer", "answerer"):
                await synapse.publish(
                    f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}",
                    agent_output_signal(
                        trace_id=new_trace_id(), parent_id=new_event_id(),
                        directed=Directed(id=nid), output={"ok": True},
                    ),
                )
            await asyncio.sleep(0.05)

            assert [(s.directed.id if s.directed else None) for s in for_answerer] == ["answerer", "answerer"]
            assert len(all_outputs) == 3
        finally:
            await orch.stop()
            await synapse.close()
    _run(run())


def test_on_agent_output_filter_by_trace_id():
    """trace_id= gates the handler to one workflow."""
    async def run():
        synapse, orch = await _make_orch()
        from cosmonapse import agent_output_signal

        target_trace = new_trace_id()
        seen = []
        try:
            @orch.on_agent_output(trace_id=target_trace)
            async def only_this_trace(sig):
                seen.append(sig)

            await orch.start()

            # Three signals on the bus; only one matches the trace filter.
            await synapse.publish(
                f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}",
                agent_output_signal(
                    trace_id=target_trace, parent_id=new_event_id(),
                    directed=Directed(id="x"), output={"a": 1},
                ),
            )
            await synapse.publish(
                f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}",
                agent_output_signal(
                    trace_id=new_trace_id(), parent_id=new_event_id(),
                    directed=Directed(id="x"), output={"a": 2},
                ),
            )
            await synapse.publish(
                f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}",
                agent_output_signal(
                    trace_id=new_trace_id(), parent_id=new_event_id(),
                    directed=Directed(id="x"), output={"a": 3},
                ),
            )
            await asyncio.sleep(0.05)

            assert len(seen) == 1
            assert seen[0].trace_id == target_trace
        finally:
            await orch.stop()
            await synapse.close()
    _run(run())


def test_on_agent_output_filter_by_capability_via_attached_axon():
    """capability= resolves through attached Axons."""
    async def run():
        synapse, orch = await _make_orch()
        from cosmonapse import agent_output_signal

        async def neuron(input, context):
            return {"ok": True}

        # Attach two axons with different capability sets.
        orch.attach_axon(Axon(
            neuron_id="summarizer", neuron_fn=neuron,
            capabilities=["summarize", "shorten"],
        ))
        orch.attach_axon(Axon(
            neuron_id="answerer", neuron_fn=neuron,
            capabilities=["qa"],
        ))
        seen = []
        try:
            @orch.on_agent_output(capability="summarize")
            async def only_summarizers(sig):
                seen.append(sig)

            await orch.start()

            for nid in ("answerer", "summarizer"):
                await synapse.publish(
                    f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}",
                    agent_output_signal(
                        trace_id=new_trace_id(), parent_id=new_event_id(),
                        directed=Directed(id=nid), output={"r": 1},
                    ),
                )
            await asyncio.sleep(0.05)

            assert [(s.directed.id if s.directed else None) for s in seen] == ["summarizer"]
        finally:
            await orch.stop()
            await synapse.close()
    _run(run())


def test_on_agent_output_capability_filter_via_registry():
    """capability= falls back to registry_store when no Axon is attached."""
    async def run():
        synapse, orch = await _make_orch(registry=True)
        from cosmonapse import agent_output_signal
        from cosmonapse.storage import NeuronRecord

        await orch.start()

        # Pre-populate the registry store with two records.
        assert orch.registry_store is not None
        await orch.registry_store.upsert(NeuronRecord(
            neuron_id="answerer", capabilities=["qa"], status="registered",
        ))
        await orch.registry_store.upsert(NeuronRecord(
            neuron_id="summarizer", capabilities=["summarize"],
            status="registered",
        ))

        seen = []
        try:
            @orch.on_agent_output(capability="summarize")
            async def only_summarizers(sig):
                seen.append(sig)

            for nid in ("answerer", "summarizer", "unknown"):
                await synapse.publish(
                    f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}",
                    agent_output_signal(
                        trace_id=new_trace_id(), parent_id=new_event_id(),
                        directed=Directed(id=nid), output={"r": 1},
                    ),
                )
            await asyncio.sleep(0.05)

            assert [(s.directed.id if s.directed else None) for s in seen] == ["summarizer"]
        finally:
            await orch.stop()
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# emit_* helpers produce well-formed envelopes
# ---------------------------------------------------------------------------


def test_emit_critique_produces_valid_envelope():
    async def run():
        synapse, orch = await _make_orch()
        seen = []
        try:
            await synapse.subscribe(
                f"cosmonapse.cog.{SignalType.CRITIQUE.value}",
                lambda s: seen.append(s),
            )
            target = new_event_id()
            await orch.emit_critique(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                target_event_id=target,
                issues=[{"type": "logic"}], verdict="fail",
            )
            await asyncio.sleep(0.01)
            assert len(seen) == 1
            sig = seen[0]
            assert sig.type is SignalType.CRITIQUE
            assert sig.payload["target_event_id"] == target
            assert sig.payload["verdict"] == "fail"
            assert sig.payload["issues"] == [{"type": "logic"}]
            assert (sig.directed.id if sig.directed else None) == orch.dendrite_id
        finally:
            await synapse.close()
    _run(run())


def test_emit_tool_call_and_result_correlate():
    async def run():
        synapse, orch = await _make_orch()
        calls, results = [], []
        try:
            await synapse.subscribe(
                f"cosmonapse.cog.{SignalType.TOOL_CALL.value}",
                lambda s: calls.append(s),
            )
            await synapse.subscribe(
                f"cosmonapse.cog.{SignalType.TOOL_RESULT.value}",
                lambda s: results.append(s),
            )
            trace = new_trace_id()
            call = await orch.emit_tool_call(
                trace_id=trace, parent_id=new_event_id(),
                tool="search", args={"q": "x"}, call_id="c1",
            )
            await orch.emit_tool_result(
                trace_id=trace, parent_id=call.id,
                tool="search", result={"hits": 3}, call_id="c1",
            )
            await asyncio.sleep(0.01)
            assert len(calls) == 1 and len(results) == 1
            assert calls[0].payload["call_id"] == results[0].payload["call_id"] == "c1"
            assert results[0].parent_id == calls[0].id
        finally:
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# on_trace narrows to one workflow across multiple types
# ---------------------------------------------------------------------------


def test_on_trace_collects_signals_for_one_workflow():
    async def run():
        synapse, orch = await _make_orch()
        target = new_trace_id()
        other = new_trace_id()
        seen: list[Signal] = []
        try:
            @orch.on_trace(target, SignalType.PLAN, SignalType.AGENT_OUTPUT)
            async def watch(sig):
                seen.append(sig)

            await orch.start()

            await orch.emit_plan(
                trace_id=target, parent_id=new_event_id(),
                steps=[{"id": 1}],
            )
            # Different trace -> filtered out.
            await orch.emit_plan(
                trace_id=other, parent_id=new_event_id(),
                steps=[{"id": 2}],
            )

            from cosmonapse import agent_output_signal
            await synapse.publish(
                f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}",
                agent_output_signal(
                    trace_id=target, parent_id=new_event_id(),
                    directed=Directed(id="n"), output={"x": 1},
                ),
            )
            await synapse.publish(
                f"cosmonapse.cog.{SignalType.AGENT_OUTPUT.value}",
                agent_output_signal(
                    trace_id=other, parent_id=new_event_id(),
                    directed=Directed(id="n"), output={"x": 2},
                ),
            )
            await asyncio.sleep(0.05)

            assert {s.type for s in seen} == {
                SignalType.PLAN, SignalType.AGENT_OUTPUT,
            }
            assert all(s.trace_id == target for s in seen)
            assert len(seen) == 2
        finally:
            await orch.stop()
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# Sanity: _handlers covers every SignalType
# ---------------------------------------------------------------------------


def test_handlers_initialized_for_every_signal_type():
    async def run():
        synapse, orch = await _make_orch()
        try:
            for t in SignalType:
                assert t in orch._handlers, (
                    f"_handlers missing entry for {t.value}"
                )
        finally:
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# Close-the-loop helpers
# ---------------------------------------------------------------------------


def test_respond_to_clarification_redispatches_task_with_lineage():
    """respond_to_clarification emits a TASK back to the asking neuron,
    preserving trace_id and chaining parent_id to the clarification."""
    from cosmonapse import clarification_signal

    async def run():
        synapse, orch = await _make_orch()
        tasks: list[Signal] = []
        try:
            await synapse.subscribe(
                f"cosmonapse.cog.{SignalType.TASK.value}",
                lambda s: tasks.append(s),
            )

            trace = new_trace_id()
            original_task_id = new_event_id()
            clar = clarification_signal(
                trace_id=trace,
                parent_id=original_task_id,
                directed=Directed(id="answerer"),
                question="Which year?",
                context={"orig_input": {"q": "How old?"}},
            )

            new_task = await orch.respond_to_clarification(
                clar, answer="2026", extra={"orig_input": {"q": "How old?"}},
            )
            await asyncio.sleep(0.02)

            assert len(tasks) == 1
            t = tasks[0]
            assert t.type is SignalType.TASK
            assert (t.directed.id if t.directed else None) == "answerer"           # back to asker
            assert t.trace_id == trace              # lineage preserved
            assert t.parent_id == clar.id           # chain to clarification
            assert new_task.id == t.id              # returned the dispatched signal
            assert t.payload["input"]["clarification"] == {
                "question": "Which year?",
                "answer": "2026",
                "orig_input": {"q": "How old?"},
            }
        finally:
            await synapse.close()
    _run(run())


def test_respond_to_clarification_neuron_override():
    """The neuron= kwarg routes the follow-up to a different target."""
    from cosmonapse import clarification_signal

    async def run():
        synapse, orch = await _make_orch()
        tasks = []
        try:
            await synapse.subscribe(
                f"cosmonapse.cog.{SignalType.TASK.value}",
                lambda s: tasks.append(s),
            )
            clar = clarification_signal(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                directed=Directed(id="asker"), question="?",
            )
            await orch.respond_to_clarification(
                clar, answer="x", neuron="reroute-target",
            )
            await asyncio.sleep(0.02)
            assert tasks and (tasks[0].directed.id if tasks[0].directed else None) == "reroute-target"
        finally:
            await synapse.close()
    _run(run())


def test_respond_to_clarification_wrong_type_raises():
    """Non-CLARIFICATION signals are rejected with DendriteProtocolError."""
    from cosmonapse import DendriteProtocolError, agent_output_signal

    async def run():
        synapse, orch = await _make_orch()
        try:
            not_a_clar = agent_output_signal(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                directed=Directed(id="x"), output={"r": 1},
            )
            with pytest.raises(DendriteProtocolError):
                await orch.respond_to_clarification(not_a_clar, answer="ok")
        finally:
            await synapse.close()
    _run(run())


def test_respond_to_escalation_routes_to_payload_target():
    """Default target is signal.payload['target']; payload includes
    reason/context/from for the receiving Neuron."""
    from cosmonapse import escalation_signal

    async def run():
        synapse, orch = await _make_orch()
        tasks: list[Signal] = []
        try:
            await synapse.subscribe(
                f"cosmonapse.cog.{SignalType.TASK.value}",
                lambda s: tasks.append(s),
            )
            trace = new_trace_id()
            esc = escalation_signal(
                trace_id=trace, parent_id=new_event_id(),
                directed=Directed(id="frontline"),
                reason="rate_limited",
                target="senior-agent",
                context={"attempts": 3},
            )
            await orch.respond_to_escalation(esc)
            await asyncio.sleep(0.02)

            assert len(tasks) == 1
            t = tasks[0]
            assert (t.directed.id if t.directed else None) == "senior-agent"
            assert t.trace_id == trace
            assert t.parent_id == esc.id
            assert t.payload["input"]["escalation"] == {
                "reason": "rate_limited",
                "context": {"attempts": 3},
                "from": "frontline",
            }
        finally:
            await synapse.close()
    _run(run())


def test_respond_to_escalation_neuron_and_input_override():
    """neuron= overrides the target; input= overrides the payload entirely."""
    from cosmonapse import escalation_signal

    async def run():
        synapse, orch = await _make_orch()
        tasks = []
        try:
            await synapse.subscribe(
                f"cosmonapse.cog.{SignalType.TASK.value}",
                lambda s: tasks.append(s),
            )
            esc = escalation_signal(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                directed=Directed(id="frontline"), reason="x", target="suggested",
            )
            await orch.respond_to_escalation(
                esc, neuron="override-target",
                input={"custom": "payload"},
            )
            await asyncio.sleep(0.02)
            t = tasks[0]
            assert (t.directed.id if t.directed else None) == "override-target"
            assert t.payload["input"] == {"custom": "payload"}
        finally:
            await synapse.close()
    _run(run())


def test_respond_to_escalation_missing_target_raises():
    """No payload.target and no neuron= override -> DendriteProtocolError."""
    from cosmonapse import DendriteProtocolError, escalation_signal

    async def run():
        synapse, orch = await _make_orch()
        try:
            esc = escalation_signal(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                directed=Directed(id="frontline"), reason="x",  # no target
            )
            with pytest.raises(DendriteProtocolError):
                await orch.respond_to_escalation(esc)
        finally:
            await synapse.close()
    _run(run())


def test_respond_to_escalation_wrong_type_raises():
    from cosmonapse import DendriteProtocolError, agent_output_signal

    async def run():
        synapse, orch = await _make_orch()
        try:
            not_an_esc = agent_output_signal(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                directed=Directed(id="x"), output={"r": 1},
            )
            with pytest.raises(DendriteProtocolError):
                await orch.respond_to_escalation(not_an_esc)
        finally:
            await synapse.close()
    _run(run())


def test_full_clarification_loop_end_to_end():
    """End-to-end: Axon asks via CLARIFICATION, orch responds, Axon gets
    a follow-up TASK with the answer, completes with AGENT_OUTPUT."""
    from cosmonapse import Axon

    async def run():
        synapse, orch = await _make_orch()
        outputs: list[Signal] = []
        ask_count = {"n": 0}

        async def asker(input, context):
            # First call: ask a clarifying question. Second call: answer.
            if "clarification" not in input:
                ask_count["n"] += 1
                return {"__clarification__": True,
                        "question": "Need a year"}
            return {"final": input["clarification"]["answer"]}

        orch.attach_axon(Axon(neuron_id="asker", neuron_fn=asker))

        @orch.on_clarification
        async def on_clar(sig):
            await orch.respond_to_clarification(sig, answer="2026")

        @orch.on_agent_output
        async def on_out(sig):
            outputs.append(sig)

        try:
            await orch.start()
            await orch.dispatch_task(neuron="asker", input={"q": "when?"})
            # Wait long enough for: TASK -> CLARIFICATION -> follow-up TASK
            # -> AGENT_OUTPUT to round-trip through MemorySynapse.
            for _ in range(20):
                await asyncio.sleep(0.02)
                if outputs:
                    break
            assert ask_count["n"] == 1
            assert len(outputs) == 1
            assert outputs[0].payload["output"] == {"final": "2026"}
        finally:
            await orch.stop()
            await synapse.close()
    _run(run())
