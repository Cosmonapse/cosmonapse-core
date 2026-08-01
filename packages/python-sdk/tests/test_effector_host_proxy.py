"""
Tests for the Effector.host deferred-decorator proxy.

Mirrors tests/test_axon_host_proxy.py and tests/test_engram_host_proxy.py.
Covers:
  * @effector.host.on_<signal>(**filters) queues at declaration time and is
    applied to the HOSTING Dendrite when it connects the Effector (start()),
    with the inbound subscription ensured.
  * Filters forward unchanged (neuron= gating on effector_id).
  * The bare decorator form (@effector.host.on_tool_call) works.
  * Invalid names fail eagerly at declaration time (AttributeError).
  * Registrations are applied exactly once per Effector.
  * The host-proxy observer is independent of (and does not interfere
    with) the Effector's own @fx.on_tool_call servicing.
"""

import asyncio
from typing import Any

import pytest

from cosmonapse import (
    Dendrite,
    Effector,
    MemorySynapse,
    SignalType,
    ToolOutcome,
    new_event_id,
    new_trace_id,
)


def _run(coro):
    return asyncio.run(coro)


class EchoEffector(Effector):
    """Toy Effector: echoes args back."""

    def __init__(self, effector_id: str = "fx", effector_kind: str = "toolbox"):
        self.effector_id = effector_id
        self.effector_kind = effector_kind
        self.capabilities: list[str] = []

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def invoke(self, tool, args, *, call_id=None, deadline_ms=None,
                     trace_id=None) -> ToolOutcome:
        return ToolOutcome(tool=tool, result={"echo": args}, call_id=call_id,
                           effector_id=self.effector_id)


def _fx(effector_id="fx", effector_kind="toolbox"):
    return EchoEffector(effector_id=effector_id, effector_kind=effector_kind)


def test_host_on_tool_call_applied_on_start_and_fires():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()

        fx = _fx()
        seen = asyncio.Event()
        got = {}

        @fx.host.on_tool_call(neuron="fx")
        async def observe(sig):
            got["payload"] = sig.payload
            seen.set()

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="fx-host", role="worker", heartbeat_s=0)
        host.attach_effector(fx)
        caller = Dendrite(synapse=synapse, namespace="hp",
                          dendrite_id="caller", heartbeat_s=0)
        try:
            await host.start()
            await caller.start()
            await caller.emit_tool_call(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                tool="echo", args={"n": 1}, call_id="c1", neuron="fx",
            )
            await asyncio.wait_for(seen.wait(), timeout=2.0)
            assert got["payload"]["tool"] == "echo"
            assert got["payload"]["call_id"] == "c1"
        finally:
            await caller.stop()
            await host.stop()
            await synapse.close()

    _run(scenario())


def test_host_filter_gates_other_effectors():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()

        fx = _fx()
        fired = []

        @fx.host.on_tool_call(neuron="fx")
        async def observe(sig):
            fired.append(sig.payload["tool"])

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="fx-host", role="worker", heartbeat_s=0)
        host.attach_effector(fx)
        caller = Dendrite(synapse=synapse, namespace="hp",
                          dendrite_id="caller", heartbeat_s=0)
        try:
            await host.start()
            await caller.start()
            await caller.emit_tool_call(
                trace_id=new_trace_id(), parent_id=new_event_id(), tool="noop",
                args={}, neuron="somewhere-else",   # not this Effector
            )
            await caller.emit_tool_call(
                trace_id=new_trace_id(), parent_id=new_event_id(), tool="echo",
                args={}, neuron="fx",
            )
            await asyncio.sleep(0.1)
            assert fired == ["echo"]
        finally:
            await caller.stop()
            await host.stop()
            await synapse.close()

    _run(scenario())


def test_host_bare_decorator_form():
    fx = _fx()

    @fx.host.on_tool_call
    async def h(sig):
        pass

    assert fx._host_regs and fx._host_regs[0][0] == "on_tool_call"
    assert fx._host_regs[0][1] is SignalType.TOOL_CALL
    assert fx._host_regs[0][2] == {}


def test_host_rejects_unknown_and_unsupported_names():
    fx = _fx()
    with pytest.raises(AttributeError):
        fx.host.on_totally_made_up
    with pytest.raises(AttributeError):
        fx.host.on_discover          # non-standard registration shape
    with pytest.raises(AttributeError):
        fx.host.invoke                # not an on_* signal decorator


def test_host_regs_applied_once():
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()
        fx = _fx()

        @fx.host.on_tool_call(neuron="fx")
        async def observe(sig):
            pass

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="fx-host", role="worker", heartbeat_s=0)
        host.attach_effector(fx)
        try:
            await host.start()
            n = len(host._handlers[SignalType.TOOL_CALL])
            await fx._on_hosted(host)          # simulated re-connect
            assert len(host._handlers[SignalType.TOOL_CALL]) == n
        finally:
            await host.stop()
            await synapse.close()

    _run(scenario())


def test_effector_registers_with_role_effector():
    """Dendrite.start() announces a hosted Effector on REGISTER with
    payload.role == "effector" - the discriminator Prism/the
    registry use to classify it distinctly from a Neuron or Engram
    (mirrors the Engram's role="engram" REGISTER)."""
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()
        fx = _fx(effector_id="fs-effector", effector_kind="filesystem")

        seen = asyncio.Event()
        got = {}

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="fx-host", role="worker", heartbeat_s=0)
        host.attach_effector(fx)
        observer = Dendrite(synapse=synapse, namespace="hp",
                            dendrite_id="observer", heartbeat_s=0)

        @observer.on_register_signal
        async def on_register(sig):
            if sig.directed and sig.directed.id == "fs-effector":
                got["sig"] = sig
                seen.set()

        try:
            await observer.start()
            await host.start()
            await asyncio.wait_for(seen.wait(), timeout=2.0)
            sig = got["sig"]
            assert sig.payload["role"] == "effector"
            assert sig.directed.type == "filesystem"
            assert sig.directed.id == "fs-effector"
        finally:
            await host.stop()
            await observer.stop()
            await synapse.close()

    _run(scenario())


def test_host_proxy_does_not_interfere_with_servicing():
    """The host-proxy observer and the Effector's own @fx.on_tool_call
    servicing are independent registries - both must fire for one call."""
    async def scenario():
        synapse = MemorySynapse()
        await synapse.connect()

        fx = _fx()
        observed = asyncio.Event()

        @fx.host.on_tool_call(neuron="fx")
        async def observe(sig):
            observed.set()

        host = Dendrite(synapse=synapse, namespace="hp",
                        dendrite_id="fx-host", role="worker", heartbeat_s=0)
        host.attach_effector(fx)
        results: list[Any] = []
        caller = Dendrite(synapse=synapse, namespace="hp",
                          dendrite_id="caller", heartbeat_s=0)

        @caller.on_tool_result
        async def collect(sig):
            results.append(sig)

        try:
            await host.start()
            await caller.start()
            await caller.emit_tool_call(
                trace_id=new_trace_id(), parent_id=new_event_id(),
                tool="echo", args={"x": 1}, call_id="c2", neuron="fx",
            )
            await asyncio.wait_for(observed.wait(), timeout=2.0)
            for _ in range(20):
                if results:
                    break
                await asyncio.sleep(0.05)
            assert results and results[0].payload["result"] == {"echo": {"x": 1}}
        finally:
            await caller.stop()
            await host.stop()
            await synapse.close()

    _run(scenario())
