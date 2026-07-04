"""
Tests for the Axon.host deferred-decorator proxy.

Covers:
  * @axon.host.on_<signal>(**filters) queues at declaration time and is
    applied to the HOSTING Dendrite when it announces the Axon, with the
    inbound subscription ensured (handler fires without ensure_subscribed).
  * Filters forward unchanged (neuron= gating).
  * The bare decorator form (@axon.host.on_agent_output) works.
  * Invalid names fail eagerly at declaration time (AttributeError).
  * Registrations are applied exactly once per Axon.
"""

import asyncio

import pytest

from cosmonapse import (
    Axon,
    Dendrite,
    MemorySynapse,
    SignalType,
    new_event_id,
    new_trace_id,
)


def _run(coro):
    return asyncio.run(coro)


async def _noop_neuron(input, context):
    return {"response": "ok"}


def _tool_axon(neuron_id="toolbox", capabilities=("hammer",)):
    return Axon(neuron_id=neuron_id, capabilities=list(capabilities),
                neuron_fn=_noop_neuron)


def test_host_on_tool_call_applied_on_announce_and_fires():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()

        axon = _tool_axon()
        seen = asyncio.Event()
        got = {}

        @axon.host.on_tool_call(neuron="hammer")
        async def call(sig):
            got["payload"] = sig.payload
            seen.set()

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="tool-node", role="worker", heartbeat_s=0)
        host.attach_axon(axon)
        caller = Dendrite(synapse=synapse, namespace="hp",
                          dendrite_id="caller", heartbeat_s=0)
        try:
            await host.start()
            await caller.start()
            await caller.emit_tool_call(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                tool="bang", args={"n": 3}, call_id="c1", neuron="hammer",
            )
            await asyncio.wait_for(seen.wait(), timeout=2.0)
            assert got["payload"]["tool"] == "bang"
            assert got["payload"]["call_id"] == "c1"
        finally:
            await caller.stop()
            await host.stop()
            await synapse.close()

    _run(scenario())


def test_host_filter_gates_other_capabilities():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()

        axon = _tool_axon()
        fired = []

        @axon.host.on_tool_call(neuron="hammer")
        async def call(sig):
            fired.append(sig.payload["tool"])

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="tool-node", role="worker", heartbeat_s=0)
        host.attach_axon(axon)
        caller = Dendrite(synapse=synapse, namespace="hp",
                          dendrite_id="caller", heartbeat_s=0)
        try:
            await host.start()
            await caller.start()
            await caller.emit_tool_call(
                trace_id=new_trace_id(), parent_id=new_event_id(), tool="saw",
                args={}, neuron="screwdriver",   # someone else's capability
            )
            await caller.emit_tool_call(
                trace_id=new_trace_id(), parent_id=new_event_id(), tool="bang",
                args={}, neuron="hammer",
            )
            await asyncio.sleep(0.1)
            assert fired == ["bang"]
        finally:
            await caller.stop()
            await host.stop()
            await synapse.close()

    _run(scenario())


def test_host_bare_decorator_form():
    axon = _tool_axon()

    @axon.host.on_agent_output
    async def h(sig):
        pass

    assert axon._host_regs and axon._host_regs[0][0] == "on_agent_output"
    assert axon._host_regs[0][1] is SignalType.AGENT_OUTPUT
    assert axon._host_regs[0][2] == {}


def test_host_rejects_unknown_and_unsupported_names():
    axon = _tool_axon()
    with pytest.raises(AttributeError):
        axon.host.on_totally_made_up
    with pytest.raises(AttributeError):
        axon.host.on_discover          # non-standard registration shape
    with pytest.raises(AttributeError):
        axon.host.before_task          # not an on_* signal decorator


def test_host_regs_applied_once():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()
        axon = _tool_axon()

        @axon.host.on_tool_call(neuron="hammer")
        async def call(sig):
            pass

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="tool-node", role="worker", heartbeat_s=0)
        host.attach_axon(axon)
        try:
            await host.start()
            n = len(host._handlers[SignalType.TOOL_CALL])
            await axon._on_register_emitted()     # simulated re-announce
            assert len(host._handlers[SignalType.TOOL_CALL]) == n
        finally:
            await host.stop()
            await synapse.close()

    _run(scenario())
