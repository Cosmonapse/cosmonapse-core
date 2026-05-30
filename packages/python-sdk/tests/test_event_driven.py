"""
Tests for the event-driven dispatch surface on Dendrite.

Covers:
  * Dendrite.capabilities aggregates from attached Axons
  * role="worker" blocks dispatch / dispatch_task / dispatch_and_wait /
    dispatch_and_subscribe / emit_final / emit_error with
    DendriteProtocolError
  * Capability-routed dispatch (no neuron, capabilities=[...]) reaches
    a Dendrite with a matching Axon
  * Subset matching: TASK requesting [X] reaches an Axon declaring [X,Y]
  * Pathway(scope="terminal") drops AGENT_OUTPUT but still receives FINAL
  * dispatch_and_subscribe returns a live Pathway the caller wires
    handlers on without awaiting
  * Back-compat: addressed dispatch_task still works exactly as before
"""

import asyncio

import pytest

from cosmonapse import (
    Axon,
    Dendrite,
    DendriteProtocolError,
    MemorySynapse,
    Pathway,
    SignalType,
)


def _run(coro):
    return asyncio.run(coro)


async def _make_synapse():
    s = MemorySynapse()
    await s.connect()
    return s


# ---------------------------------------------------------------------------
# Dendrite.capabilities aggregate
# ---------------------------------------------------------------------------


def test_dendrite_capabilities_is_union_of_axon_caps():
    async def run():
        s = await _make_synapse()
        d = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def n(i, c): return {}
            assert d.capabilities == []
            d.attach_axon(Axon(neuron_id="a", neuron_fn=n,
                               capabilities=["summarize", "english"]))
            d.attach_axon(Axon(neuron_id="b", neuron_fn=n,
                               capabilities=["english", "translate"]))
            # Dedup + sorted
            assert d.capabilities == ["english", "summarize", "translate"]
        finally:
            await s.close()
    _run(run())


def test_capabilities_recomputed_on_detach():
    async def run():
        s = await _make_synapse()
        d = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def n(i, c): return {}
            d.attach_axon(Axon(neuron_id="a", neuron_fn=n,
                               capabilities=["x", "y"]))
            d.attach_axon(Axon(neuron_id="b", neuron_fn=n,
                               capabilities=["y", "z"]))
            assert d.capabilities == ["x", "y", "z"]
            await d.detach_axon("a")
            assert d.capabilities == ["y", "z"]
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# role guard
# ---------------------------------------------------------------------------


def test_worker_role_rejects_dispatch_methods():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        try:
            async with worker:
                with pytest.raises(DendriteProtocolError):
                    await worker.dispatch_task(neuron="x", input={})
                with pytest.raises(DendriteProtocolError):
                    await worker.dispatch(neuron="x", input={})
                with pytest.raises(DendriteProtocolError):
                    await worker.dispatch_and_wait(
                        neuron="x", input={}, timeout_s=0.1,
                    )
                with pytest.raises(DendriteProtocolError):
                    await worker.dispatch_and_subscribe(
                        neuron="x", input={},
                    )
        finally:
            await s.close()
    _run(run())


def test_orchestrator_role_can_dispatch():
    async def run():
        s = await _make_synapse()
        orch = Dendrite(synapse=s, namespace="t", role="orchestrator",
                        heartbeat_s=0)
        try:
            async with orch:
                # dispatch_task without a target raises ValueError, not
                # DendriteProtocolError — the role guard passes, the
                # arg validation catches it.
                with pytest.raises(ValueError):
                    await orch.dispatch_task(input={})
        finally:
            await s.close()
    _run(run())


def test_worker_role_blocks_all_emit_helpers():
    """The role guard sits on emit(), so every cognition emitter
    (emit_final / emit_plan / emit_critique / ...) raises on a
    worker — not just the dispatch* methods."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        try:
            async with worker:
                tid = "trc_" + "0" * 26
                pid = "evt_" + "0" * 26
                with pytest.raises(DendriteProtocolError):
                    await worker.emit_final(
                        trace_id=tid, parent_id=pid, result={},
                    )
                with pytest.raises(DendriteProtocolError):
                    await worker.emit_error(
                        trace_id=tid, parent_id=pid,
                        code="X", message="x",
                    )
                with pytest.raises(DendriteProtocolError):
                    await worker.emit_plan(
                        trace_id=tid, parent_id=pid, steps=[],
                    )
                with pytest.raises(DendriteProtocolError):
                    await worker.emit_critique(
                        trace_id=tid, parent_id=pid,
                        target_event_id=pid, issues=[], verdict="pass",
                    )
        finally:
            await s.close()
    _run(run())


def test_worker_can_still_serve_tasks():
    """The role guard MUST NOT break worker Axon replies. A worker
    hosting an Axon should still respond to addressed TASKs because
    Axon.handle_task publishes via _publish, bypassing emit()."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def n(i, c): return {"got": i.get("v")}
            worker.attach_axon(Axon(neuron_id="w", neuron_fn=n))
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    neuron="w", input={"v": 1}, timeout_s=2.0,
                )
                assert sig.type is SignalType.AGENT_OUTPUT
                assert sig.payload["output"] == {"got": 1}
        finally:
            await s.close()
    _run(run())


def test_default_role_is_orchestrator():
    async def run():
        s = await _make_synapse()
        d = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            assert d.role == "orchestrator"
        finally:
            await s.close()
    _run(run())


def test_invalid_role_raises():
    async def run():
        s = await _make_synapse()
        try:
            with pytest.raises(ValueError):
                Dendrite(synapse=s, namespace="t", role="boss",
                         heartbeat_s=0)
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Capability-routed dispatch
# ---------------------------------------------------------------------------


def test_capability_routed_task_reaches_matching_dendrite():
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def summarize(i, c):
                return {"summary": f"sum:{i.get('text', '')}"}

            worker.attach_axon(Axon(
                neuron_id="summarizer", neuron_fn=summarize,
                capabilities=["summarize", "english"],
            ))
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    capabilities=["summarize"],
                    input={"text": "hello"},
                    timeout_s=2.0,
                )
                assert sig.type is SignalType.AGENT_OUTPUT
                assert sig.payload["output"]["summary"] == "sum:hello"
                # Reply was emitted by the Axon, so neuron field is the
                # Axon's neuron_id, not the addressed name (there was none).
                assert sig.neuron == "summarizer"
        finally:
            await s.close()
    _run(run())


def test_capability_subset_matching():
    """TASK requesting a subset of an Axon's caps matches."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def n(i, c): return {"ok": True}
            worker.attach_axon(Axon(
                neuron_id="multi", neuron_fn=n,
                capabilities=["x", "y", "z"],
            ))
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    capabilities=["x"], input={}, timeout_s=2.0,
                )
                assert sig.type is SignalType.AGENT_OUTPUT
        finally:
            await s.close()
    _run(run())


def test_capability_routed_no_match_times_out():
    """A capability-routed TASK with no matching Axon anywhere on the
    bus never gets a reply — wait() times out."""
    async def run():
        s = await _make_synapse()
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        try:
            async def n(i, c): return {}
            worker.attach_axon(Axon(
                neuron_id="x", neuron_fn=n, capabilities=["foo"],
            ))
            async with worker, orch:
                with pytest.raises(asyncio.TimeoutError):
                    await orch.dispatch_and_wait(
                        capabilities=["nonexistent"],
                        input={}, timeout_s=0.1,
                    )
        finally:
            await s.close()
    _run(run())


def test_dispatch_requires_neuron_or_capabilities():
    async def run():
        s = await _make_synapse()
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with orch:
                with pytest.raises(ValueError):
                    await orch.dispatch(input={})
                with pytest.raises(ValueError):
                    await orch.dispatch_task(input={})
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Pathway scope
# ---------------------------------------------------------------------------


def test_pathway_scope_terminal_drops_agent_output():
    """With scope="terminal", AGENT_OUTPUT (intermediate) is dropped
    from the Pathway. Only CLARIFICATION / ERROR / FINAL pass through.
    Note: AGENT_OUTPUT is NOT a terminal type for the Pathway, so it
    won't auto-close — the orchestrator stays subscribed for the real
    terminal events.
    """
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def n(i, c): return {"x": 1}
            worker.attach_axon(Axon(
                neuron_id="n", neuron_fn=n, capabilities=["go"],
            ))
            async with worker, orch:
                pw = await orch.dispatch(
                    capabilities=["go"], input={},
                    scope="terminal",
                )
                assert pw.scope == "terminal"
                # Wait briefly — the AGENT_OUTPUT will fly past but be
                # dropped by the scope filter. wait() should time out
                # because AGENT_OUTPUT is filtered out and no FINAL/
                # ERROR/CLARIFICATION arrives.
                with pytest.raises(asyncio.TimeoutError):
                    await pw.wait(timeout_s=0.2)
                await pw.close()
        finally:
            await s.close()
    _run(run())


def test_pathway_scope_terminal_passes_final():
    """A FINAL emitted on the trace still reaches a scope="terminal"
    Pathway and auto-closes it."""
    async def run():
        s = await _make_synapse()
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with orch:
                pw = await orch.dispatch(
                    neuron="nobody", input={},
                    scope="terminal",
                )
                # Emit a FINAL on the same trace from the orchestrator.
                await orch.emit_final(
                    trace_id=pw.trace_id,
                    parent_id="evt_" + "0" * 26,
                    result={"done": True},
                )
                sig = await pw.wait(timeout_s=1.0)
                assert sig.type is SignalType.FINAL
                # FINAL closed the Pathway.
                for _ in range(20):
                    if pw.closed: break
                    await asyncio.sleep(0.01)
                assert pw.closed
        finally:
            await s.close()
    _run(run())


def test_pathway_scope_all_is_default():
    async def run():
        s = await _make_synapse()
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with orch:
                pw = await orch.dispatch(neuron="x", input={})
                assert pw.scope == "all"
                await pw.close()
        finally:
            await s.close()
    _run(run())


def test_pathway_invalid_scope_raises():
    with pytest.raises(ValueError):
        Pathway(trace_id="trc_" + "0" * 26, scope="weird")


# ---------------------------------------------------------------------------
# dispatch_and_subscribe
# ---------------------------------------------------------------------------


def test_dispatch_and_subscribe_returns_live_pathway():
    """dispatch_and_subscribe returns a Pathway; caller can attach
    handlers and they fire as signals arrive."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def n(i, c): return {"value": i.get("v")}
            worker.attach_axon(Axon(
                neuron_id="n", neuron_fn=n, capabilities=["go"],
            ))
            async with worker, orch:
                pw = await orch.dispatch_and_subscribe(
                    capabilities=["go"], input={"v": 42},
                )
                assert isinstance(pw, Pathway)
                assert not pw.closed

                received = []
                @pw.on(SignalType.AGENT_OUTPUT)
                async def collect(sig):
                    received.append(sig.payload["output"])

                # Give the AGENT_OUTPUT a chance to arrive.
                await asyncio.sleep(0.1)
                assert received == [{"value": 42}]
                await pw.close()
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Role guard on emit() (covers all cognition emit_* helpers)
# ---------------------------------------------------------------------------


def test_worker_role_blocks_all_emit_helpers():
    """The role guard sits on emit(), so every cognition emitter
    (emit_final / emit_plan / emit_critique / ...) raises on a
    worker - not just the dispatch* methods."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        try:
            async with worker:
                tid = "trc_" + "0" * 26
                pid = "evt_" + "0" * 26
                with pytest.raises(DendriteProtocolError):
                    await worker.emit_final(
                        trace_id=tid, parent_id=pid, result={},
                    )
                with pytest.raises(DendriteProtocolError):
                    await worker.emit_error(
                        trace_id=tid, parent_id=pid,
                        code="X", message="x",
                    )
                with pytest.raises(DendriteProtocolError):
                    await worker.emit_plan(
                        trace_id=tid, parent_id=pid, steps=[],
                    )
                with pytest.raises(DendriteProtocolError):
                    await worker.emit_critique(
                        trace_id=tid, parent_id=pid,
                        target_event_id=pid, issues=[], verdict="pass",
                    )
        finally:
            await s.close()
    _run(run())


def test_worker_can_still_serve_tasks():
    """The role guard MUST NOT break worker Axon replies. A worker
    hosting an Axon should still respond to addressed TASKs because
    Axon.handle_task publishes via _publish, bypassing emit()."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def n(i, c): return {"got": i.get("v")}
            worker.attach_axon(Axon(neuron_id="w", neuron_fn=n))
            async with worker, orch:
                sig = await orch.dispatch_and_wait(
                    neuron="w", input={"v": 1}, timeout_s=2.0,
                )
                assert sig.type is SignalType.AGENT_OUTPUT
                assert sig.payload["output"] == {"got": 1}
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Once-only capability-routed delivery (queue group on .TASK.routed)
# ---------------------------------------------------------------------------


def test_capability_routed_delivered_once_across_identical_workers():
    """Two workers with identical Axon cap profiles share a queue group
    on the routed subject. A single capability-routed TASK must be
    processed by exactly one of them, not both."""
    async def run():
        s = await _make_synapse()
        worker_a = Dendrite(synapse=s, namespace="t", role="worker",
                            dendrite_id="worker_a", heartbeat_s=0)
        worker_b = Dendrite(synapse=s, namespace="t", role="worker",
                            dendrite_id="worker_b", heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            processed_by: list[str] = []

            async def make_neuron(label):
                async def n(i, c):
                    processed_by.append(label)
                    return {"who": label}
                return n

            worker_a.attach_axon(Axon(
                neuron_id="a", neuron_fn=await make_neuron("a"),
                capabilities=["summarize"],
            ))
            worker_b.attach_axon(Axon(
                neuron_id="b", neuron_fn=await make_neuron("b"),
                capabilities=["summarize"],
            ))

            async with worker_a, worker_b, orch:
                # Single capability-routed dispatch.
                sig = await orch.dispatch_and_wait(
                    capabilities=["summarize"],
                    input={"text": "hello"},
                    timeout_s=2.0,
                )
                # Exactly one worker processed it.
                assert sig.type is SignalType.AGENT_OUTPUT
                assert len(processed_by) == 1, (
                    f"expected once-only delivery, got "
                    f"{len(processed_by)}: {processed_by}"
                )
        finally:
            await s.close()
    _run(run())


def test_addressed_task_still_broadcasts_after_routed_subject_split():
    """Splitting subjects must not break addressed delivery: an
    addressed TASK targeted at an Axon hosted only on worker_b must
    still reach worker_b even when worker_a is up with no matching
    Axon."""
    async def run():
        s = await _make_synapse()
        worker_a = Dendrite(synapse=s, namespace="t", role="worker",
                            dendrite_id="worker_a", heartbeat_s=0)
        worker_b = Dendrite(synapse=s, namespace="t", role="worker",
                            dendrite_id="worker_b", heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def a_only(i, c): return {"who": "a-only"}
            async def b_only(i, c): return {"who": "b-only"}

            worker_a.attach_axon(Axon(neuron_id="ax", neuron_fn=a_only))
            worker_b.attach_axon(Axon(neuron_id="bx", neuron_fn=b_only))

            async with worker_a, worker_b, orch:
                sig = await orch.dispatch_and_wait(
                    neuron="bx", input={}, timeout_s=2.0,
                )
                assert sig.type is SignalType.AGENT_OUTPUT
                assert sig.payload["output"] == {"who": "b-only"}
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# TASK_OFFER / BID / TASK_AWARDED
# ---------------------------------------------------------------------------


def test_dispatch_offer_first_bid_wins():
    """Producer broadcasts TASK_OFFER, two workers bid, the first
    bidder wins TASK_AWARDED and processes the work."""
    async def run():
        s = await _make_synapse()
        worker_a = Dendrite(synapse=s, namespace="t", role="worker",
                            dendrite_id="wa", heartbeat_s=0)
        worker_b = Dendrite(synapse=s, namespace="t", role="worker",
                            dendrite_id="wb", heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            processed_by: list[str] = []

            async def na(i, c):
                processed_by.append("a"); return {"who": "a"}
            async def nb(i, c):
                processed_by.append("b"); return {"who": "b"}

            worker_a.attach_axon(Axon(
                neuron_id="aa", neuron_fn=na, capabilities=["summarize"],
            ))
            worker_b.attach_axon(Axon(
                neuron_id="bb", neuron_fn=nb, capabilities=["summarize"],
            ))

            @worker_a.on_task_offer
            async def bid_a(sig):
                await worker_a.bid(sig, neuron="aa", cost=10.0)

            @worker_b.on_task_offer
            async def bid_b(sig):
                # Stall slightly so worker_a bids first.
                await asyncio.sleep(0.02)
                await worker_b.bid(sig, neuron="bb", cost=5.0)

            async with worker_a, worker_b, orch:
                pw = await orch.dispatch_offer(
                    input={"text": "x"},
                    capabilities=["summarize"],
                    deadline_ms=200,
                    select="first_bid",
                )
                sig = await pw.wait(timeout_s=2.0)
                assert sig.type is SignalType.AGENT_OUTPUT
                # first_bid → worker_a wins
                assert processed_by == ["a"]
                assert sig.payload["output"] == {"who": "a"}
        finally:
            await s.close()
    _run(run())


def test_dispatch_offer_lowest_cost_wins():
    """With select='lowest_cost', the lowest-cost bidder wins regardless
    of bid arrival order."""
    async def run():
        s = await _make_synapse()
        worker_high = Dendrite(synapse=s, namespace="t", role="worker",
                               dendrite_id="hi", heartbeat_s=0)
        worker_low = Dendrite(synapse=s, namespace="t", role="worker",
                              dendrite_id="lo", heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            processed_by: list[str] = []

            async def nh(i, c):
                processed_by.append("high"); return {"who": "high"}
            async def nl(i, c):
                processed_by.append("low"); return {"who": "low"}

            worker_high.attach_axon(Axon(
                neuron_id="hh", neuron_fn=nh, capabilities=["plan"],
            ))
            worker_low.attach_axon(Axon(
                neuron_id="ll", neuron_fn=nl, capabilities=["plan"],
            ))

            @worker_high.on_task_offer
            async def bid_h(sig):
                # Bids fast but expensive.
                await worker_high.bid(sig, neuron="hh", cost=100.0)

            @worker_low.on_task_offer
            async def bid_l(sig):
                # Bids slow but cheap.
                await asyncio.sleep(0.03)
                await worker_low.bid(sig, neuron="ll", cost=1.0)

            async with worker_high, worker_low, orch:
                pw = await orch.dispatch_offer(
                    input={}, capabilities=["plan"],
                    deadline_ms=150, select="lowest_cost",
                )
                sig = await pw.wait(timeout_s=2.0)
                assert sig.type is SignalType.AGENT_OUTPUT
                assert processed_by == ["low"]
        finally:
            await s.close()
    _run(run())


def test_dispatch_offer_no_bids_times_out():
    """If no worker bids before the deadline, dispatch_offer raises
    TimeoutError."""
    async def run():
        s = await _make_synapse()
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with orch:
                with pytest.raises(TimeoutError):
                    await orch.dispatch_offer(
                        input={}, capabilities=["nobody-has-this"],
                        deadline_ms=50, select="first_bid",
                    )
        finally:
            await s.close()
    _run(run())


def test_worker_can_bid_despite_role():
    """bid() bypasses the orchestrator guard - workers must be able
    to compete in capability routing."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def n(i, c): return {"ok": True}
            worker.attach_axon(Axon(
                neuron_id="w", neuron_fn=n, capabilities=["go"],
            ))

            bid_count = 0
            @worker.on_task_offer
            async def respond(sig):
                nonlocal bid_count
                # Worker must be able to bid even though role="worker".
                await worker.bid(sig, neuron="w", cost=1.0)
                bid_count += 1

            async with worker, orch:
                pw = await orch.dispatch_offer(
                    input={}, capabilities=["go"],
                    deadline_ms=100, select="first_bid",
                )
                sig = await pw.wait(timeout_s=2.0)
                assert bid_count == 1
                assert sig.type is SignalType.AGENT_OUTPUT
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Back-compat
# ---------------------------------------------------------------------------


def test_addressed_dispatch_task_still_works():
    """The original addressed dispatch_task path is unchanged."""
    async def run():
        s = await _make_synapse()
        worker = Dendrite(synapse=s, namespace="t", role="worker",
                          heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async def echo(i, c): return {"y": i["y"]}
            worker.attach_axon(Axon(neuron_id="echo", neuron_fn=echo))

            seen = []
            @orch.on_agent_output
            async def collect(sig): seen.append(sig)

            async with worker, orch:
                emitted = await orch.dispatch_task(
                    neuron="echo", input={"y": 5},
                )
                assert emitted.type is SignalType.TASK
                await asyncio.sleep(0.1)
                assert len(seen) == 1
                assert seen[0].payload["output"] == {"y": 5}
        finally:
            await s.close()
    _run(run())
