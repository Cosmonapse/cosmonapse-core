"""
Terminal-handler finalize (release decision: option (b)).

A default Axon only ever emits AGENT_OUTPUT - it never emits FINAL - so a
``dispatch(scope="terminal")`` Pathway would hang against stock workers.
The fix: the dispatching side tags the TASK (``payload.finalize``, set
automatically when ``scope="terminal"``), and the worker Dendrite that ran
the addressed/routed Axon promotes a successful AGENT_OUTPUT by also
emitting FINAL on the trace.

Covers:
  * dispatch_and_wait(scope="terminal") resolves with FINAL against a
    default worker; FINAL.result == the AGENT_OUTPUT payload; lineage is
    TASK -> AGENT_OUTPUT -> FINAL (FINAL parented to the AGENT_OUTPUT,
    attributed to the producing neuron).
  * Default scope="all" dispatch emits NO FINAL (multi-step orchestration
    keeps owning workflow conclusion - no premature Pathway close).
  * Explicit finalize=True on an all-scope dispatch emits FINAL.
  * Explicit finalize=False on a terminal-scope dispatch suppresses
    promotion (another peer owns FINAL).
  * ERROR replies are not promoted.
  * CLARIFICATION replies are not promoted (workflow is paused, not done).
  * Capability-routed dispatch promotes the same way.
  * dispatch_offer(scope="terminal") propagates the tag through
    TASK_AWARDED and the winner's Dendrite promotes.
  * dispatch_task(finalize=True) tags the raw TASK payload.
"""

import asyncio

import pytest

from cosmonapse import Axon, Dendrite, MemorySynapse, SignalType


def _run(coro):
    return asyncio.run(coro)


async def _make_synapse():
    s = MemorySynapse()
    await s.connect()
    return s


async def _echo(i, c):
    return {"echo": i.get("text", "")}


def _pair(s, *, caps=None):
    worker = Dendrite(synapse=s, namespace="t", role="worker",
                      dendrite_id="w", heartbeat_s=0)
    worker.attach_axon(Axon(
        neuron_id="echoer", neuron_fn=_echo,
        capabilities=caps or ["echo"],
    ))
    orch = Dendrite(synapse=s, namespace="t", dendrite_id="o",
                    heartbeat_s=0)
    return worker, orch


# ---------------------------------------------------------------------------
# The headline behaviour
# ---------------------------------------------------------------------------


def test_terminal_scope_resolves_with_final_against_default_worker():
    async def run():
        s = await _make_synapse()
        worker, orch = _pair(s)
        try:
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    neuron="echoer", input={"text": "hi"},
                    scope="terminal", timeout_s=2.0,
                )
                assert sig.type is SignalType.FINAL
                assert sig.payload["result"] == {"echo": "hi"}
                # Attributed to the producing neuron.
                assert sig.directed is not None
                assert sig.directed.id == "echoer"
        finally:
            await s.close()
    _run(run())


def test_final_is_parented_to_the_agent_output():
    async def run():
        s = await _make_synapse()
        worker, orch = _pair(s)
        try:
            async with worker, orch:
                pw = await orch.dispatch(
                    neuron="echoer", input={"text": "x"}, finalize=True,
                )
                out = await pw.wait_for(SignalType.AGENT_OUTPUT, timeout_s=2.0)
                fin = await pw.wait_for(SignalType.FINAL, timeout_s=2.0)
                assert fin.parent_id == out.id
                assert fin.trace_id == out.trace_id
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Defaults stay safe for multi-step orchestration
# ---------------------------------------------------------------------------


def test_all_scope_default_emits_no_final():
    async def run():
        s = await _make_synapse()
        worker, orch = _pair(s)
        try:
            async with worker, orch:
                pw = await orch.dispatch(neuron="echoer", input={"text": "x"})
                await pw.wait_for(SignalType.AGENT_OUTPUT, timeout_s=2.0)
                with pytest.raises(asyncio.TimeoutError):
                    await pw.wait_for(SignalType.FINAL, timeout_s=0.3)
                assert not pw.closed  # no premature auto-close
                await pw.close()
        finally:
            await s.close()
    _run(run())


def test_explicit_finalize_true_on_all_scope():
    async def run():
        s = await _make_synapse()
        worker, orch = _pair(s)
        try:
            async with worker, orch:
                pw = await orch.dispatch(
                    neuron="echoer", input={"text": "x"},
                    scope="all", finalize=True,
                )
                fin = await pw.wait_for(SignalType.FINAL, timeout_s=2.0)
                assert fin.payload["result"] == {"echo": "x"}
        finally:
            await s.close()
    _run(run())


def test_explicit_finalize_false_on_terminal_scope_suppresses_promotion():
    async def run():
        s = await _make_synapse()
        worker, orch = _pair(s)
        try:
            async with worker, orch:
                with pytest.raises(asyncio.TimeoutError):
                    await orch.dispatch_and_wait(
                        neuron="echoer", input={"text": "x"},
                        scope="terminal", finalize=False, timeout_s=0.5,
                    )
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Only AGENT_OUTPUT is promoted
# ---------------------------------------------------------------------------


def test_error_reply_is_not_promoted():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)

        async def boom(i, c):
            raise RuntimeError("nope")

        worker.attach_axon(Axon(neuron_id="bad", neuron_fn=boom))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with worker, orch:
                pw = await orch.dispatch(
                    neuron="bad", input={}, finalize=True,
                )
                err = await pw.wait(timeout_s=2.0)
                assert err.type is SignalType.ERROR
                # ERROR already auto-closed the Pathway; no FINAL followed.
                assert pw.closed
        finally:
            await s.close()
    _run(run())


def test_clarification_reply_is_not_promoted():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)

        async def asks(i, c):
            return {"__clarification__": True, "question": "which one?"}

        worker.attach_axon(Axon(neuron_id="asker", neuron_fn=asks))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with worker, orch:
                pw = await orch.dispatch(
                    neuron="asker", input={}, scope="terminal",
                )
                sig = await pw.wait(timeout_s=2.0)
                assert sig.type is SignalType.CLARIFICATION
                with pytest.raises(asyncio.TimeoutError):
                    await pw.wait_for(SignalType.FINAL, timeout_s=0.3)
                await pw.close()
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Routing variants
# ---------------------------------------------------------------------------


def test_capability_routed_terminal_scope_promotes():
    async def run():
        s = await _make_synapse()
        worker, orch = _pair(s, caps=["echo", "english"])
        try:
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    capabilities=["echo"], input={"text": "cap"},
                    scope="terminal", timeout_s=2.0,
                )
                assert sig.type is SignalType.FINAL
                assert sig.payload["result"] == {"echo": "cap"}
        finally:
            await s.close()
    _run(run())


def test_dispatch_offer_terminal_scope_promotes_through_award():
    async def run():
        s = await _make_synapse()
        worker, orch = _pair(s)
        try:
            async with worker, orch:
                @worker.on_task_offer
                async def _bid(offer):
                    await worker.bid(offer, neuron="echoer", cost=1.0)

                pw = await orch.dispatch_offer(
                    input={"text": "won"}, capabilities=["echo"],
                    deadline_ms=300, scope="terminal",
                )
                fin = await pw.wait(timeout_s=2.0)
                assert fin.type is SignalType.FINAL
                assert fin.payload["result"] == {"echo": "won"}
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Envelope-level tagging
# ---------------------------------------------------------------------------


def test_dispatch_task_finalize_tags_payload():
    async def run():
        s = await _make_synapse()
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with orch:
                tagged = await orch.dispatch_task(
                    neuron="x", input={}, finalize=True,
                )
                assert tagged.payload.get("finalize") is True
                untagged = await orch.dispatch_task(neuron="x", input={})
                assert "finalize" not in untagged.payload
        finally:
            await s.close()
    _run(run())
