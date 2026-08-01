"""
Tests for the stable-protocol release gap fixes.

Covers:
  * Generic @on_signal + the new named decorators (on_final,
    on_task_declined, on_clarification_answer / on_permission_decision)
  * ensure_subscribed() removes the late-registration race
  * await_decision() resolves on discrete CLARIFICATION_ANSWER /
    PERMISSION_DECISION via op-pathway correlation
  * @axon.before_task transforms or rejects TASK input
  * attach_axon on a running Dendrite raises; add_axon hot-attaches live
  * auto_bid: a stock worker answers TASK_OFFER out of the box;
    a user on_task_offer handler suppresses the default bidder
  * _BaseNeuron renders clarification/permission follow-up TASKs into
    prompt continuations (the close-the-loop defaults fix)
  * COSMO_INTENT_SYSTEM_PROMPT injection rules in Axon.from_source
  * Envelope protocol-version validation (major 1 accepted, others not)
  * Registry staleness: _sweep_stale_neurons + find_neurons(max_age_s=...)
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from cosmonapse import (
    Axon,
    Dendrite,
    DendriteProtocolError,
    MemoryRegistryStore,
    MemorySynapse,
    Signal,
    SignalType,
)
from cosmonapse._neuron_base import _BaseNeuron
from cosmonapse.axon import COSMO_INTENT_SYSTEM_PROMPT
from cosmonapse.storage.base import NeuronRecord


def _run(coro):
    return asyncio.run(coro)


async def _make_synapse():
    s = MemorySynapse()
    await s.connect()
    return s


async def _echo(i, c):
    return {"echo": i.get("text", "")}


# ---------------------------------------------------------------------------
# Decorator surface
# ---------------------------------------------------------------------------


def test_on_final_and_generic_on_signal_fire():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        worker.attach_axon(Axon(neuron_id="e", neuron_fn=_echo))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        named, generic = [], []

        @orch.on_final
        async def _f(sig):
            named.append(sig)

        @orch.on_signal(SignalType.FINAL)
        async def _g(sig):
            generic.append(sig)

        try:
            async with worker, orch:
                await orch.dispatch_and_wait(
                    neuron="e", input={"text": "x"},
                    scope="terminal", timeout_s=2.0,
                )
                await asyncio.sleep(0.05)
                assert len(named) == 1 and named[0].type is SignalType.FINAL
                assert len(generic) == 1
        finally:
            await s.close()
    _run(run())


def test_on_task_declined_fires_for_losing_bidder():
    async def run():
        s = await _make_synapse()
        a = Dendrite(synapse=s, namespace="t", role="worker", heartbeat_s=0,
                     auto_bid=False)
        b = Dendrite(synapse=s, namespace="t", role="worker", heartbeat_s=0,
                     auto_bid=False)
        async def n(i, c): return {"ok": True}
        a.attach_axon(Axon(neuron_id="a", neuron_fn=n, capabilities=["x"]))
        b.attach_axon(Axon(neuron_id="b", neuron_fn=n, capabilities=["x"]))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        declined = []
        try:
            @a.on_task_offer
            async def _bid_a(offer):
                await a.bid(offer, neuron="a", cost=1.0)

            @b.on_task_offer
            async def _bid_b(offer):
                await b.bid(offer, neuron="b", cost=9.0)

            @b.on_task_declined(neuron="b")
            async def _lost(sig):
                declined.append(sig)

            async with a, b, orch:
                # lowest_cost drains the full deadline so both bids are
                # collected and the loser is explicitly declined.
                pw = await orch.dispatch_offer(
                    input={}, capabilities=["x"], deadline_ms=200,
                    select="lowest_cost",
                )
                await pw.wait(timeout_s=2.0)
                await pw.close()
                await asyncio.sleep(0.05)
                assert len(declined) == 1
                assert declined[0].payload.get("reason") == "not selected"
        finally:
            await s.close()
    _run(run())


def test_ensure_subscribed_after_start():
    async def run():
        s = await _make_synapse()
        d = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        got = []
        try:
            async with d:
                @d.on_signal(SignalType.PLAN)
                async def _h(sig):
                    got.append(sig)
                await d.ensure_subscribed(SignalType.PLAN)
                await d.emit_plan(
                    trace_id="trc_" + "0" * 26,
                    parent_id="evt_" + "0" * 26,
                    steps=["a"],
                )
                await asyncio.sleep(0.05)
                assert len(got) == 1
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# await_decision (discrete answer-path consumers)
# ---------------------------------------------------------------------------


def test_await_decision_clarification_roundtrip():
    async def run():
        s = await _make_synapse()
        asker = Dendrite(synapse=s, namespace="t", role="worker",
                         dendrite_id="asker", heartbeat_s=0)
        responder = Dendrite(synapse=s, namespace="t",
                             dendrite_id="resp", heartbeat_s=0)

        async def needs_info(i, c):
            return {"__clarification__": True, "question": "which?"}

        asker.attach_axon(Axon(neuron_id="q", neuron_fn=needs_info))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            @responder.on_clarification
            async def _answer(sig):
                await responder.answer_clarification(sig, answer="the blue one")

            async with asker, responder, orch:
                pw = await orch.dispatch(neuron="q", input={})
                clar = await pw.wait(timeout_s=2.0)
                assert clar.type is SignalType.CLARIFICATION
                ans = await orch.await_decision(clar, timeout_s=2.0)
                assert ans.type is SignalType.CLARIFICATION_ANSWER
                assert ans.parent_id == clar.id
                assert ans.payload.get("answer") == "the blue one"
                await pw.close()
        finally:
            await s.close()
    _run(run())


def test_await_decision_permission_and_type_guard():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)

        async def needs_perm(i, c):
            return {"__permission__": True, "action": "rm -rf"}

        worker.attach_axon(Axon(neuron_id="p", neuron_fn=needs_perm))
        responder = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            @responder.on_permission
            async def _decide(sig):
                await responder.deny_permission(sig, reason="too risky")

            async with worker, responder, orch:
                pw = await orch.dispatch(neuron="p", input={})
                req = await pw.wait(timeout_s=2.0)
                assert req.type is SignalType.PERMISSION
                verdict = await orch.await_decision(req, timeout_s=2.0)
                assert verdict.type is SignalType.PERMISSION_DECISION
                assert verdict.payload.get("granted") is False
                await pw.close()

                with pytest.raises(DendriteProtocolError):
                    await orch.await_decision(verdict)
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Axon before_task + hot attach
# ---------------------------------------------------------------------------


def test_before_task_transforms_input():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        ax = Axon(neuron_id="e", neuron_fn=_echo)

        @ax.before_task
        def upcase(input_data):
            return {"text": input_data.get("text", "").upper()}

        worker.attach_axon(ax)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    neuron="e", input={"text": "hi"}, timeout_s=2.0,
                )
                assert sig.payload["output"]["echo"] == "HI"
        finally:
            await s.close()
    _run(run())


def test_before_task_rejection_becomes_error():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        ax = Axon(neuron_id="e", neuron_fn=_echo)

        @ax.before_task
        async def reject(input_data):
            raise ValueError("input not allowed")

        worker.attach_axon(ax)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    neuron="e", input={}, timeout_s=2.0,
                )
                assert sig.type is SignalType.ERROR
                assert "input not allowed" in sig.payload["message"]
        finally:
            await s.close()
    _run(run())


def test_attach_axon_on_running_dendrite_raises():
    async def run():
        s = await _make_synapse()
        d = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with d:
                with pytest.raises(RuntimeError, match="add_axon"):
                    d.attach_axon(Axon(neuron_id="late", neuron_fn=_echo))
        finally:
            await s.close()
    _run(run())


def test_add_axon_hot_attach_receives_tasks():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with worker, orch:
                await worker.add_axon(Axon(
                    neuron_id="late", neuron_fn=_echo,
                    capabilities=["echo"],
                ))
                # addressed
                sig = await orch.dispatch_and_wait(
                    neuron="late", input={"text": "a"}, timeout_s=2.0,
                )
                assert sig.payload["output"]["echo"] == "a"
                # capability-routed (queue group was created on hot attach)
                sig2 = await orch.dispatch_and_wait(
                    capabilities=["echo"], input={"text": "b"}, timeout_s=2.0,
                )
                assert sig2.payload["output"]["echo"] == "b"
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Auto-bid defaults
# ---------------------------------------------------------------------------


def test_stock_worker_auto_bids_on_offer():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        worker.attach_axon(Axon(
            neuron_id="e", neuron_fn=_echo, capabilities=["echo"],
        ))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with worker, orch:
                pw = await orch.dispatch_offer(
                    input={"text": "auto"}, capabilities=["echo"],
                    deadline_ms=300, scope="terminal",
                )
                fin = await pw.wait(timeout_s=2.0)
                assert fin.type is SignalType.FINAL
                assert fin.payload["result"] == {"echo": "auto"}
        finally:
            await s.close()
    _run(run())


def test_auto_bid_disabled_or_capability_mismatch_does_not_bid():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        worker.attach_axon(Axon(
            neuron_id="e", neuron_fn=_echo, capabilities=["echo"],
        ))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with worker, orch:
                with pytest.raises(TimeoutError):
                    await orch.dispatch_offer(
                        input={}, capabilities=["not-echo"],
                        deadline_ms=80,
                    )
        finally:
            await s.close()
    _run(run())


def test_user_offer_handler_suppresses_auto_bid():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        worker.attach_axon(Axon(
            neuron_id="e", neuron_fn=_echo, capabilities=["echo"],
        ))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        bids = []
        try:
            @worker.on_task_offer
            async def _custom(offer):
                pass  # deliberately never bids

            @orch.on_bid
            async def _seen(sig):
                bids.append(sig)

            async with worker, orch:
                with pytest.raises(TimeoutError):
                    await orch.dispatch_offer(
                        input={}, capabilities=["echo"], deadline_ms=80,
                    )
                assert bids == []
        finally:
            await s.close()
    _run(run())


def test_filtered_offer_handler_bids():
    """Regression: @on_task_offer(capability=...) must still fire and bid.

    The capability filter used to resolve the offer's *directed neuron*,
    but a TASK_OFFER is a broadcast carrying its capabilities in the
    payload  -  so the filter silently swallowed every offer and no BID
    was ever sent (the 10-bidding example timed out)."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        worker.attach_axon(Axon(
            neuron_id="e", neuron_fn=_echo, capabilities=["echo"],
        ))
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        bids = []
        try:
            @worker.on_task_offer(capability="echo")
            async def _respond(offer):
                await worker.bid(offer, neuron="e", cost=0.001,
                                 confidence=0.9)

            @orch.on_bid
            async def _seen(sig):
                bids.append(sig)

            async with worker, orch:
                pw = await orch.dispatch_offer(
                    input={"text": "hi"}, capabilities=["echo"],
                    deadline_ms=250, select="lowest_cost",
                )
                sig = await pw.wait(timeout_s=5.0)
                assert len(bids) == 1
                assert sig.directed and sig.directed.id == "e"
                assert sig.payload["output"]["echo"] == "hi"
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Built-in Neuron follow-up rendering
# ---------------------------------------------------------------------------


def test_followup_prompt_clarification():
    p = _BaseNeuron._followup_prompt({
        "clarification": {"question": "which region?", "answer": "eu-west-1"},
    })
    assert p is not None
    assert "which region?" in p and "eu-west-1" in p
    assert "Continue the original task" in p


def test_followup_prompt_permission_denied():
    p = _BaseNeuron._followup_prompt({
        "permission": {"action": "delete", "granted": False,
                       "reason": "nope"},
    })
    assert p is not None
    assert "DENIED" in p and "delete" in p and "nope" in p


def test_followup_prompt_passthrough_none():
    assert _BaseNeuron._followup_prompt({"prompt": "hi"}) is None
    assert _BaseNeuron._followup_prompt({}) is None


def test_require_input_accepts_clarification_followup():
    n = _BaseNeuron()
    prompt, messages = n._require_input(
        {"clarification": {"question": "q", "answer": "a"}}, "Test",
    )
    assert messages is None and prompt and "q" in prompt
    with pytest.raises(ValueError):
        n._require_input({"unrelated": 1}, "Test")


# ---------------------------------------------------------------------------
# Intent system prompt injection
# ---------------------------------------------------------------------------


def test_from_source_injects_intent_prompt():
    pytest.importorskip("httpx", reason="httpx not installed")
    ax = Axon.from_source("ollama", neuron_id="m", model="llama3")
    assert COSMO_INTENT_SYSTEM_PROMPT in ax._fn.system  # type: ignore[attr-defined]


def test_from_source_appends_to_existing_system():
    pytest.importorskip("httpx", reason="httpx not installed")
    ax = Axon.from_source("ollama", neuron_id="m", model="llama3",
                          system="You are terse.")
    assert ax._fn.system.startswith("You are terse.")  # type: ignore[attr-defined]
    assert COSMO_INTENT_SYSTEM_PROMPT in ax._fn.system  # type: ignore[attr-defined]


def test_from_source_teach_intents_opt_out():
    pytest.importorskip("httpx", reason="httpx not installed")
    ax = Axon.from_source("ollama", neuron_id="m", model="llama3",
                          teach_intents=False)
    assert ax._fn.system is None  # type: ignore[attr-defined]


def test_from_source_hf_not_taught_by_default():
    pytest.importorskip("httpx", reason="httpx not installed")
    # huggingface accepts no system= kwarg; default must not inject.
    ax = Axon.from_source("huggingface", neuron_id="m",
                          endpoint="http://localhost:8080")
    assert not hasattr(ax._fn, "system") or ax._fn.system is None
    with pytest.raises(ValueError, match="teach_intents"):
        Axon.from_source("huggingface", neuron_id="m2",
                         endpoint="http://localhost:8080",
                         teach_intents=True)


# ---------------------------------------------------------------------------
# Protocol version validation
# ---------------------------------------------------------------------------


def test_envelope_rejects_other_major_version():
    base = dict(type=SignalType.TASK, payload={"input": {}})
    assert Signal(v="1", **base).v == "1"
    assert Signal(v="1.3", **base).v == "1.3"
    with pytest.raises(ValueError):
        Signal(v="2", **base)
    with pytest.raises(ValueError):
        Signal.decode(Signal(**base).encode().replace(b'"v":"1"', b'"v":"2"'))


# ---------------------------------------------------------------------------
# Registry staleness
# ---------------------------------------------------------------------------


def test_sweep_marks_stale_neurons_deregistered():
    async def run():
        s = await _make_synapse()
        store = MemoryRegistryStore()
        d = Dendrite(synapse=s, namespace="t", registry_store=store,
                     heartbeat_s=30.0)  # stale_after defaults to 90s
        try:
            await store.connect()
            now = datetime.now(UTC)
            await store.upsert(NeuronRecord(
                neuron_id="ghost",
                last_heartbeat=now - timedelta(seconds=600),
            ))
            await store.upsert(NeuronRecord(
                neuron_id="alive",
                last_heartbeat=now,
            ))
            await d._sweep_stale_neurons(now)
            live = {r.neuron_id for r in await store.list()}
            assert "alive" in live and "ghost" not in live
        finally:
            await s.close()
    _run(run())


def test_find_neurons_max_age_filter():
    async def run():
        s = await _make_synapse()
        store = MemoryRegistryStore()
        d = Dendrite(synapse=s, namespace="t", registry_store=store,
                     heartbeat_s=0)
        try:
            await store.connect()
            now = datetime.now(UTC)
            await store.upsert(NeuronRecord(
                neuron_id="old", last_heartbeat=now - timedelta(seconds=120),
            ))
            await store.upsert(NeuronRecord(
                neuron_id="fresh", last_heartbeat=now,
            ))
            all_recs = {r.neuron_id for r in await d.find_neurons()}
            fresh = {r.neuron_id for r in await d.find_neurons(max_age_s=60)}
            assert all_recs == {"old", "fresh"}
            assert fresh == {"fresh"}
        finally:
            await s.close()
    _run(run())
