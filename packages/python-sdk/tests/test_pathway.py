"""
Tests for the Pathway primitive and the dispatch() / dispatch_and_wait() /
observe_pathway() surface on Dendrite.

The suite exercises the three consumption shapes (wait / callbacks /
async iteration), the originator vs observer roles, auto-close on FINAL
or ERROR, idempotent close, timeout semantics, and Dendrite.stop()
cleanup. It also pins down that Pathway is purely additive: the
existing dispatch_task / on_agent_output path is unchanged.
"""

import asyncio

import pytest

from cosmonapse import (
    Axon,
    Dendrite,
    MemorySynapse,
    Pathway,
    PathwayClosedError,
    SignalType,
    new_trace_id,
)


def _run(coro):
    return asyncio.run(coro)


async def _make_pair(namespace="t"):
    """Set up an orchestrator + worker pair on the same Synapse."""
    synapse = MemorySynapse()
    await synapse.connect()
    worker = Dendrite(synapse=synapse, namespace=namespace, heartbeat_s=0)
    orch = Dendrite(synapse=synapse, namespace=namespace, heartbeat_s=0)
    return synapse, worker, orch


# ---------------------------------------------------------------------------
# Shape #1: wait()
# ---------------------------------------------------------------------------


def test_dispatch_and_wait_returns_agent_output():
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def echo(inp, ctx):
                return {"echo": inp["q"]}

            worker.attach_axon(Axon(neuron_id="echo", neuron_fn=echo))
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    neuron="echo", input={"q": "hello"}, timeout_s=2.0,
                )
                assert sig.type is SignalType.AGENT_OUTPUT
                assert sig.payload["output"] == {"echo": "hello"}
        finally:
            await synapse.close()
    _run(run())


def test_pathway_wait_blocks_until_agent_output():
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def slow(inp, ctx):
                await asyncio.sleep(0.05)
                return {"value": 42}

            worker.attach_axon(Axon(neuron_id="slow", neuron_fn=slow))
            async with worker, orch:
                pw = await orch.dispatch(neuron="slow", input={})
                assert isinstance(pw, Pathway)
                assert pw.role == "originator"
                assert not pw.closed
                sig = await pw.wait(timeout_s=2.0)
                assert sig.type is SignalType.AGENT_OUTPUT
                assert sig.payload["output"] == {"value": 42}
                await pw.close()
        finally:
            await synapse.close()
    _run(run())


def test_wait_timeout_raises():
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            async with orch:
                pw = await orch.dispatch(neuron="nobody", input={})
                with pytest.raises(asyncio.TimeoutError):
                    await pw.wait(timeout_s=0.05)
                await pw.close()
        finally:
            await synapse.close()
    _run(run())


def test_wait_for_filters_by_type():
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def needs_more(inp, ctx):
                return {"__clarification__": True, "question": "which one?"}

            worker.attach_axon(Axon(neuron_id="ask", neuron_fn=needs_more))
            async with worker, orch:
                pw = await orch.dispatch(neuron="ask", input={})
                sig = await pw.wait_for(SignalType.CLARIFICATION, timeout_s=2.0)
                assert sig.type is SignalType.CLARIFICATION
                assert sig.payload["question"] == "which one?"
                await pw.close()
        finally:
            await synapse.close()
    _run(run())


def test_wait_resolves_on_error_signal():
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def boom(inp, ctx):
                raise RuntimeError("nope")

            worker.attach_axon(Axon(neuron_id="boom", neuron_fn=boom))
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    neuron="boom", input={}, timeout_s=2.0,
                )
                assert sig.type is SignalType.ERROR
                assert sig.payload["code"] == "NEURON_EXCEPTION"
        finally:
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# Shape #2: callbacks (@pw.on)
# ---------------------------------------------------------------------------


def test_pathway_on_callback_fires():
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def echo(inp, ctx):
                return {"x": inp["x"]}

            worker.attach_axon(Axon(neuron_id="echo", neuron_fn=echo))
            async with worker, orch:
                pw = await orch.dispatch(neuron="echo", input={"x": 1})
                seen: list = []

                @pw.on(SignalType.AGENT_OUTPUT)
                async def collect(sig):
                    seen.append(sig.payload["output"])

                await pw.wait(timeout_s=2.0)
                # Allow the callback (scheduled via gather in _deliver)
                # one event-loop tick to complete - though the current
                # implementation awaits inline, this is a defensive sleep.
                await asyncio.sleep(0)
                assert seen == [{"x": 1}]
                await pw.close()
        finally:
            await synapse.close()
    _run(run())


def test_pathway_callback_and_wait_both_see_signal():
    """Broadcast semantics: callbacks, wait(), and iteration all see
    every signal independently. None of them drain the others."""
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def neuron(inp, ctx):
                return {"ok": True}

            worker.attach_axon(Axon(neuron_id="n", neuron_fn=neuron))
            async with worker, orch:
                pw = await orch.dispatch(neuron="n", input={})
                cb_count = 0

                @pw.on(SignalType.AGENT_OUTPUT)
                async def _(sig):
                    nonlocal cb_count
                    cb_count += 1

                sig = await pw.wait(timeout_s=2.0)
                assert sig.type is SignalType.AGENT_OUTPUT
                assert cb_count == 1
                await pw.close()
        finally:
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# Shape #3: async iteration
# ---------------------------------------------------------------------------


def test_async_for_yields_signals_then_stops_on_close():
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def echo(inp, ctx):
                return {"x": 1}

            worker.attach_axon(Axon(neuron_id="echo", neuron_fn=echo))
            async with worker, orch, await orch.dispatch(neuron="echo", input={}) as pw:
                received = []
                # Iterate in a background task so we can close from outside.
                async def consume():
                    async for sig in pw:
                        received.append(sig)
                        # Stop after the first AGENT_OUTPUT (mirrors a
                        # workflow that breaks out once it has its result).
                        if sig.type is SignalType.AGENT_OUTPUT:
                            break

                await asyncio.wait_for(consume(), timeout=2.0)
                assert len(received) >= 1
                assert received[-1].type is SignalType.AGENT_OUTPUT
        finally:
            await synapse.close()
    _run(run())


def test_async_for_terminates_on_pathway_close():
    """An async-for loop should exit cleanly when the Pathway closes,
    not hang indefinitely."""
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            async with orch:
                pw = await orch.dispatch(neuron="nobody", input={})
                received = []

                async def consume():
                    async for sig in pw:
                        received.append(sig)

                consumer = asyncio.create_task(consume())
                await asyncio.sleep(0.05)
                await pw.close()
                await asyncio.wait_for(consumer, timeout=1.0)
                # No signals received, but the loop terminated cleanly.
                assert received == []
        finally:
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# Auto-close on terminal types
# ---------------------------------------------------------------------------


def test_pathway_auto_closes_on_final():
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            async with orch:
                pw = await orch.dispatch(neuron="nobody", input={})
                # Emit FINAL on the same trace from the orchestrator itself.
                await orch.emit_final(
                    trace_id=pw.trace_id, parent_id="evt_x" * 4,
                    result={"done": True},
                )
                # Auto-close happens during _deliver; one tick is enough.
                for _ in range(20):
                    if pw.closed:
                        break
                    await asyncio.sleep(0.01)
                assert pw.closed
        finally:
            await synapse.close()
    _run(run())


def test_pathway_auto_closes_on_error():
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def boom(inp, ctx):
                raise RuntimeError("x")

            worker.attach_axon(Axon(neuron_id="boom", neuron_fn=boom))
            async with worker, orch:
                pw = await orch.dispatch(neuron="boom", input={})
                sig = await pw.wait(timeout_s=2.0)
                assert sig.type is SignalType.ERROR
                # Give the auto-close a moment to flip the flag after
                # _deliver returns.
                for _ in range(20):
                    if pw.closed:
                        break
                    await asyncio.sleep(0.01)
                assert pw.closed
        finally:
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# Observer role
# ---------------------------------------------------------------------------


def test_observe_pathway_sees_signals_from_other_dendrite():
    """A Dendrite that didn't originate a trace can still observe it
    via observe_pathway(trace_id)."""
    async def run():
        synapse, worker, orch_a = await _make_pair()
        orch_b = Dendrite(synapse=synapse, namespace="t",
                          dendrite_id="b", heartbeat_s=0)
        try:
            async def echo(inp, ctx):
                return {"who": "echo"}

            worker.attach_axon(Axon(neuron_id="echo", neuron_fn=echo))
            async with worker, orch_a, orch_b:
                # orch_a originates the trace
                pw_a = await orch_a.dispatch(neuron="echo", input={})
                # orch_b observes the same trace
                pw_b = await orch_b.observe_pathway(pw_a.trace_id)
                assert pw_b.role == "observer"

                sig_a = await pw_a.wait(timeout_s=2.0)
                sig_b = await pw_b.wait(timeout_s=2.0)
                assert sig_a.id == sig_b.id
                assert sig_b.payload["output"] == {"who": "echo"}
                await pw_a.close()
                await pw_b.close()
        finally:
            await synapse.close()
    _run(run())


def test_observe_pathway_rejects_duplicate():
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            async with orch:
                tid = new_trace_id()
                pw = await orch.observe_pathway(tid)
                with pytest.raises(ValueError):
                    await orch.observe_pathway(tid)
                await pw.close()
        finally:
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_close_is_idempotent():
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            async with orch:
                pw = await orch.dispatch(neuron="nobody", input={})
                await pw.close()
                assert pw.closed
                # Second close must not raise.
                await pw.close()
                assert pw.closed
        finally:
            await synapse.close()
    _run(run())


def test_wait_on_closed_pathway_raises():
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            async with orch:
                pw = await orch.dispatch(neuron="nobody", input={})
                await pw.close()
                with pytest.raises(PathwayClosedError):
                    await pw.wait(timeout_s=0.1)
        finally:
            await synapse.close()
    _run(run())


def test_pending_wait_fails_when_pathway_closes():
    """A wait() blocked at the time close() is called should resolve
    with PathwayClosedError, not hang forever."""
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            async with orch:
                pw = await orch.dispatch(neuron="nobody", input={})

                async def waiter():
                    return await pw.wait(timeout_s=5.0)

                task = asyncio.create_task(waiter())
                await asyncio.sleep(0.05)
                await pw.close()
                with pytest.raises(PathwayClosedError):
                    await asyncio.wait_for(task, timeout=1.0)
        finally:
            await synapse.close()
    _run(run())


def test_dendrite_stop_closes_open_pathways():
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            await orch.start()
            pw = await orch.dispatch(neuron="nobody", input={})
            assert not pw.closed
            await orch.stop()
            assert pw.closed
        finally:
            await synapse.close()
    _run(run())


def test_close_evicts_from_dendrite_registry():
    async def run():
        synapse, _worker, orch = await _make_pair()
        try:
            async with orch:
                pw = await orch.dispatch(neuron="nobody", input={})
                tid = pw.trace_id
                assert tid in orch._pathways
                await pw.close()
                assert tid not in orch._pathways
        finally:
            await synapse.close()
    _run(run())


# ---------------------------------------------------------------------------
# Additive guarantee: existing dispatch_task path is untouched
# ---------------------------------------------------------------------------


def test_dispatch_task_still_works_independently_of_pathway():
    """Pathway is opt-in additive: dispatch_task() must continue to
    return the emitted Signal envelope without opening a Pathway."""
    async def run():
        synapse, worker, orch = await _make_pair()
        try:
            async def echo(inp, ctx):
                return {"y": inp["y"]}

            worker.attach_axon(Axon(neuron_id="echo", neuron_fn=echo))
            seen = []

            @orch.on_agent_output
            async def collect(sig):
                seen.append(sig)

            async with worker, orch:
                emitted = await orch.dispatch_task(
                    neuron="echo", input={"y": 7},
                )
                assert emitted.type is SignalType.TASK
                await asyncio.sleep(0.1)
                assert len(seen) == 1
                assert seen[0].payload["output"] == {"y": 7}
                # And the Dendrite's Pathway registry should be empty -
                # dispatch_task does NOT open one.
                assert orch._pathways == {}
        finally:
            await synapse.close()
    _run(run())
