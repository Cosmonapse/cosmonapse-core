"""
Tests for the Engram.host deferred-decorator proxy.

Mirrors tests/test_axon_host_proxy.py. Covers:
  * @engram.host.on_<signal>(**filters) queues at declaration time and is
    applied to the HOSTING Dendrite when it connects the Engram (start()),
    with the inbound subscription ensured (handler fires without a
    hand-wired ``ensure_subscribed`` call).
  * Filters forward unchanged.
  * The bare decorator form (@engram.host.on_imprint_signal) works.
  * Invalid names fail eagerly at declaration time (AttributeError).
  * Registrations are applied exactly once per Engram.
"""

import asyncio

import pytest

from cosmonapse import (
    Dendrite,
    InMemoryEngram,
    MemorySynapse,
    SignalType,
    new_event_id,
    new_trace_id,
)


def _run(coro):
    return asyncio.run(coro)


def _engram(engram_id="session-memory", engram_kind="context"):
    return InMemoryEngram(engram_id=engram_id, engram_kind=engram_kind)


def test_host_on_imprint_signal_applied_on_start_and_fires():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()

        engram = _engram()
        seen = asyncio.Event()
        got = {}

        @engram.host.on_imprint_signal
        async def persist(sig):
            got["entry"] = sig.payload.get("entry")
            seen.set()

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="mem-host", role="worker", heartbeat_s=0)
        host.attach_engram(engram)
        caller = Dendrite(synapse=synapse, namespace="hp",
                          dendrite_id="caller", heartbeat_s=0)
        try:
            await host.start()
            await caller.start()
            await caller.imprint(
                engram_id=engram.engram_id, op="add",
                entry={"content": "hello", "tags": ["summary"]},
                trace_id=new_trace_id(), parent_id=new_event_id(),
            )
            await asyncio.wait_for(seen.wait(), timeout=2.0)
            assert got["entry"]["content"] == "hello"
        finally:
            await caller.stop()
            await host.stop()
            await synapse.close()

    _run(scenario())


def test_host_filter_gates_other_neurons():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()

        engram = _engram()
        fired = []

        @engram.host.on_imprint_signal(neuron="writer")
        async def persist(sig):
            fired.append(sig.payload["entry"]["content"])

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="mem-host", role="worker", heartbeat_s=0)
        host.attach_engram(engram)
        caller = Dendrite(synapse=synapse, namespace="hp",
                          dendrite_id="caller", heartbeat_s=0)
        try:
            await host.start()
            await caller.start()
            # A neuron= filter narrows against sig.directed.id; imprint()
            # addresses the engram itself, so pass neuron= explicitly via
            # meta-less direct emit to exercise the gate deterministically.
            await caller.imprint(
                engram_id=engram.engram_id, op="add",
                entry={"content": "from someone else"},
                trace_id=new_trace_id(), parent_id=new_event_id(),
            )
            await asyncio.sleep(0.1)
            assert fired == []   # directed.id is the engram_id, not "writer"
        finally:
            await caller.stop()
            await host.stop()
            await synapse.close()

    _run(scenario())


def test_host_bare_decorator_form():
    engram = _engram()

    @engram.host.on_imprint_signal
    async def h(sig):
        pass

    assert engram._host_regs and engram._host_regs[0][0] == "on_imprint_signal"
    assert engram._host_regs[0][1] is SignalType.IMPRINT
    assert engram._host_regs[0][2] == {}


def test_host_rejects_unknown_and_unsupported_names():
    engram = _engram()
    with pytest.raises(AttributeError):
        engram.host.on_totally_made_up
    with pytest.raises(AttributeError):
        engram.host.on_discover          # non-standard registration shape
    with pytest.raises(AttributeError):
        engram.host.recall               # not an on_* signal decorator


def test_host_regs_applied_once():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()
        engram = _engram()

        @engram.host.on_imprint_signal
        async def persist(sig):
            pass

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="mem-host", role="worker", heartbeat_s=0)
        host.attach_engram(engram)
        try:
            await host.start()
            n = len(host._handlers[SignalType.IMPRINT])
            await engram._on_hosted(host)          # simulated re-connect
            assert len(host._handlers[SignalType.IMPRINT]) == n
        finally:
            await host.stop()
            await synapse.close()

    _run(scenario())
