"""
Tests for workflow STOP / cooperative cancellation, the Engram saga
(compensating-log rollback), and RetryStrategy.

Covers:
  * default_retry_on predicate (timeout / recoverable ERROR / closed pathway)
  * stop_signal / stopped_signal payload shape
  * Engram saga: add/upsert/delete journaling -> compensate reverses;
    commit discards the journal
  * STOP cancels an in-flight neuron and the worker acks with STOPPED
  * run_with_retry: succeeds after a stalled first attempt (preempted by
    STOP); raises TimeoutError when every attempt is stuck

Runnable two ways: under pytest, or standalone (``python tests/test_stop_retry_saga.py``)
so it can be exercised without the pytest dependency.
"""

import asyncio

from cosmonapse import (
    Axon,
    Dendrite,
    InMemoryEngram,
    MemorySynapse,
    PathwayClosedError,
    RetryStrategy,
    SignalType,
    default_retry_on,
    error_signal,
    final_signal,
    stop_signal,
    stopped_signal,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Unit: retry predicate + signal payloads
# ---------------------------------------------------------------------------


def test_default_retry_on_predicate():
    assert default_retry_on(asyncio.TimeoutError()) is True
    assert default_retry_on(PathwayClosedError("x")) is True
    rec = error_signal(trace_id="trc_a", parent_id=None, code="E",
                       message="m", recoverable=True)
    assert default_retry_on(rec) is True
    non = error_signal(trace_id="trc_a", parent_id=None, code="E",
                      message="m", recoverable=False)
    assert default_retry_on(non) is False
    fin = final_signal(trace_id="trc_a", parent_id=None, result={"x": 1})
    assert default_retry_on(fin) is False


def test_stop_signal_payloads():
    s = stop_signal(trace_id="trc_a", rollback=True, reason="why")
    assert s.type is SignalType.STOP
    assert s.payload == {"rollback": True, "reason": "why"}
    a = stopped_signal(trace_id="trc_a", parent_id=None, node="ns",
                      rolled_back=True, cancelled=2, compensated=3)
    assert a.type is SignalType.STOPPED
    assert a.payload["cancelled"] == 2 and a.payload["compensated"] == 3
    assert a.payload["rolled_back"] is True


# ---------------------------------------------------------------------------
# Unit: Engram saga journal
# ---------------------------------------------------------------------------


def test_saga_add_then_compensate_removes():
    async def run():
        eng = InMemoryEngram(engram_id="e")
        await eng.imprint("add", {"id": "x1", "content": "v"}, trace_id="trc_1")
        assert any(e["id"] == "x1" for e in eng._snapshot())
        n = await eng.compensate("trc_1")
        assert n == 1
        assert not any(e["id"] == "x1" for e in eng._snapshot())
    _run(run())


def test_saga_upsert_then_compensate_restores_prior():
    async def run():
        eng = InMemoryEngram(engram_id="e")
        # committed baseline (no trace -> not journaled)
        await eng.imprint("upsert", {"content": "v1"}, merge_key="k")
        # journaled overwrite
        await eng.imprint("upsert", {"content": "v2"}, merge_key="k",
                          trace_id="trc_2")
        assert [e["content"] for e in eng._snapshot()] == ["v2"]
        await eng.compensate("trc_2")
        assert [e["content"] for e in eng._snapshot()] == ["v1"]
    _run(run())


def test_saga_delete_then_compensate_restores():
    async def run():
        eng = InMemoryEngram(engram_id="e")
        await eng.imprint("add", {"id": "d1", "content": "keep"})  # committed
        await eng.imprint("delete", {"id": "d1"}, trace_id="trc_3")
        assert not any(e["id"] == "d1" for e in eng._snapshot())
        await eng.compensate("trc_3")
        snap = eng._snapshot()
        assert any(e["id"] == "d1" and e["content"] == "keep" for e in snap)
    _run(run())


def test_saga_commit_discards_journal():
    async def run():
        eng = InMemoryEngram(engram_id="e")
        await eng.imprint("add", {"id": "c1", "content": "v"}, trace_id="trc_4")
        await eng.commit("trc_4")
        n = await eng.compensate("trc_4")
        assert n == 0
        assert any(e["id"] == "c1" for e in eng._snapshot())
    _run(run())


# ---------------------------------------------------------------------------
# Integration: STOP cancels an in-flight neuron + STOPPED ack
# ---------------------------------------------------------------------------


def test_stop_cancels_in_flight_neuron_and_acks():
    async def run():
        s = MemorySynapse()
        await s.connect()
        worker = Dendrite(synapse=s, namespace="t", role="worker", heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)

        started = asyncio.Event()
        state = {"cancelled": False, "completed": False}

        async def slow(i, c):
            started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            state["completed"] = True
            return {"done": True}

        worker.attach_axon(Axon(neuron_id="slow", neuron_fn=slow,
                                capabilities=["x"]))
        try:
            async with worker, orch:
                pw = await orch.dispatch(neuron="slow", input={})
                await asyncio.wait_for(started.wait(), 2.0)
                acks = await orch.stop_trace(pw.trace_id, collect_acks=True,
                                             timeout_s=0.5)
                await asyncio.sleep(0.05)
                assert state["cancelled"] is True
                assert state["completed"] is False
                assert pw.closed is True
                assert any(a.payload.get("cancelled", 0) >= 1 for a in acks), \
                    f"no worker STOPPED ack with cancelled>=1: {[a.payload for a in acks]}"
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Integration: retry
# ---------------------------------------------------------------------------


def test_run_with_retry_succeeds_after_stalled_attempt():
    async def run():
        s = MemorySynapse()
        await s.connect()
        worker = Dendrite(synapse=s, namespace="t", role="worker", heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)

        calls = {"n": 0}

        async def flaky(i, c):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(5)   # stalls -> abandoned + STOPped
                return {"late": True}
            return {"ok": calls["n"]}

        worker.attach_axon(Axon(neuron_id="flaky", neuron_fn=flaky,
                                capabilities=["x"]))
        try:
            async with worker, orch:
                sig = await orch.run_with_retry(
                    retry=RetryStrategy(max_attempts=3, timeout_s=0.4),
                    neuron="flaky", input={},
                )
                assert sig.type is SignalType.AGENT_OUTPUT, sig.type
                assert sig.payload["output"]["ok"] == 2
                assert calls["n"] == 2
        finally:
            await s.close()
    _run(run())


def test_run_with_retry_exhausts_and_raises_timeout():
    async def run():
        s = MemorySynapse()
        await s.connect()
        worker = Dendrite(synapse=s, namespace="t", role="worker", heartbeat_s=0)
        orch = Dendrite(synapse=s, namespace="t", heartbeat_s=0)

        async def hang(i, c):
            await asyncio.sleep(10)
            return {}

        worker.attach_axon(Axon(neuron_id="hang", neuron_fn=hang,
                                capabilities=["x"]))
        raised = None
        try:
            async with worker, orch:
                try:
                    await orch.run_with_retry(
                        retry=RetryStrategy(max_attempts=2, timeout_s=0.3),
                        neuron="hang", input={},
                    )
                except asyncio.TimeoutError as exc:
                    raised = exc
            assert raised is not None
        finally:
            await s.close()
    _run(run())


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
        except Exception:
            failed += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"ok    {t.__name__}")
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
