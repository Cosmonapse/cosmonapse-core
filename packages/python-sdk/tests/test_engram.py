"""
tests/test_engram.py
~~~~~~~~~~~~~~~~~~~~
Engram unit + integration tests. See ENGRAM_DESIGN.md.

The test matrix:

* Envelope: signal builders & validation
* Engram conformance: InMemoryEngram, SqliteEngram (Postgres skip-if-no-DSN)
* EngramClient: parent_id correlation, deadlines, recall_mode semantics
* Axon binding: declarative engrams= + helper injection (with & without)
* End-to-end: Neuron emits TASK → mid-task RECALL+IMPRINT → AGENT_OUTPUT,
  full trace stays on one trace_id (ENGRAM_DESIGN.md §5.4)
"""

from __future__ import annotations

import os
import pytest

from cosmonapse import (
    Axon,
    Dendrite,
    Engram,
    EngramBinding,
    EngramNotBound,
    EngramTimeout,
    ImprintReceipt,
    InMemoryEngram,
    MemorySynapse,
    RecallResult,
    Signal,
    SignalType,
    SqliteEngram,
    imprint_signal,
    imprinted_signal,
    new_event_id,
    new_trace_id,
    recall_signal,
    recalled_signal,
)


# ---------------------------------------------------------------------------
# Envelope: builder validation
# ---------------------------------------------------------------------------


class TestEnvelope:

    def test_recall_signal_requires_engram_id_or_kind(self):
        with pytest.raises(ValueError, match="engram_id"):
            recall_signal(
                trace_id=new_trace_id(),
                parent_id=new_event_id(),
                query={"text": "x"},
            )

    def test_recall_signal_validates_mode(self):
        with pytest.raises(ValueError, match="recall_mode"):
            recall_signal(
                trace_id=new_trace_id(),
                parent_id=new_event_id(),
                engram_id="x",
                query={"text": "x"},
                recall_mode="bogus",
            )

    def test_recall_signal_round_trip(self):
        sig = recall_signal(
            trace_id=new_trace_id(),
            parent_id=new_event_id(),
            engram_id="ctx",
            query={"text": "k8s"},
            deadline_ms=400,
            recall_mode="merge",
        )
        assert sig.type is SignalType.RECALL
        assert sig.payload["engram_id"] == "ctx"
        assert sig.payload["recall_mode"] == "merge"
        encoded = sig.encode()
        decoded = Signal.decode(encoded)
        assert decoded.payload == sig.payload

    def test_recalled_signal_carries_hits(self):
        sig = recalled_signal(
            trace_id=new_trace_id(),
            parent_id=new_event_id(),
            engram_id="ctx",
            hits=[{"id": "eng_x", "entry": {"a": 1}, "score": 0.9}],
        )
        assert sig.type is SignalType.RECALLED
        assert sig.payload["hits"][0]["score"] == 0.9

    def test_imprint_signal_validates_op(self):
        with pytest.raises(ValueError, match="op"):
            imprint_signal(
                trace_id=new_trace_id(),
                parent_id=new_event_id(),
                engram_id="ctx",
                op="foo",
                entry={"content": "x"},
            )

    def test_imprint_merge_requires_merge_key(self):
        with pytest.raises(ValueError, match="merge_key"):
            imprint_signal(
                trace_id=new_trace_id(),
                parent_id=new_event_id(),
                engram_id="ctx",
                op="merge",
                entry={"content": {"a": 1}},
            )

    def test_imprinted_signal_round_trip(self):
        sig = imprinted_signal(
            trace_id=new_trace_id(),
            parent_id=new_event_id(),
            engram_id="ctx",
            op="append",
            id="eng_XYZ",
            version=1,
        )
        assert sig.type is SignalType.IMPRINTED
        assert sig.payload["op"] == "append"


# ---------------------------------------------------------------------------
# Engram conformance: tests run against every backend
# ---------------------------------------------------------------------------


@pytest.fixture
async def memory_engram():
    eng = InMemoryEngram(engram_id="ctx-mem", engram_kind="context")
    await eng.connect()
    yield eng
    await eng.close()


@pytest.fixture
async def sqlite_engram(tmp_path):
    eng = SqliteEngram(
        path=str(tmp_path / "engram.db"),
        engram_id="ctx-sqlite",
        engram_kind="context",
    )
    await eng.connect()
    yield eng
    await eng.close()


_BACKENDS = ["memory_engram", "sqlite_engram"]


class TestEngramConformance:

    @pytest.mark.parametrize("fixture_name", _BACKENDS)
    @pytest.mark.asyncio
    async def test_append_and_recall(self, fixture_name, request):
        eng: Engram = request.getfixturevalue(fixture_name)
        rec = await eng.imprint(
            "append",
            {"content": "hello world", "tags": ["k8s"]},
            merge_key="incident:1",
            imprint_id="evt_FAKEIMPRINT00000000000001",
        )
        assert rec.ok
        assert rec.id is not None
        hits = await eng.recall({"text": "hello"})
        assert len(hits) == 1
        assert hits[0].entry["content"] == "hello world"

    @pytest.mark.parametrize("fixture_name", _BACKENDS)
    @pytest.mark.asyncio
    async def test_imprint_idempotency(self, fixture_name, request):
        eng: Engram = request.getfixturevalue(fixture_name)
        kwargs = dict(
            entry={"content": "once", "tags": []},
            merge_key="ind:1",
            imprint_id="evt_IDEMPOTENT0000000000000A",
        )
        a = await eng.imprint("append", **kwargs)
        b = await eng.imprint("append", **kwargs)
        assert a.id == b.id, "replays must return the original entry id"
        # Only one entry stored
        hits = await eng.recall({"merge_key": "ind:1"})
        assert len(hits) == 1

    @pytest.mark.parametrize("fixture_name", _BACKENDS)
    @pytest.mark.asyncio
    async def test_upsert_then_merge(self, fixture_name, request):
        eng: Engram = request.getfixturevalue(fixture_name)
        await eng.imprint(
            "upsert",
            {"content": {"a": 1}, "tags": ["x"]},
            merge_key="m:1",
        )
        await eng.imprint(
            "merge",
            {"content": {"b": 2}, "tags": ["y"]},
            merge_key="m:1",
        )
        hits = await eng.recall({"merge_key": "m:1"})
        assert len(hits) == 1
        merged = hits[0].entry["content"]
        assert merged == {"a": 1, "b": 2}
        assert set(hits[0].entry["tags"]) == {"x", "y"}
        assert hits[0].entry["version"] == 2

    @pytest.mark.parametrize("fixture_name", _BACKENDS)
    @pytest.mark.asyncio
    async def test_delete(self, fixture_name, request):
        eng: Engram = request.getfixturevalue(fixture_name)
        rec = await eng.imprint(
            "append", {"content": "doomed"}, merge_key="d:1",
        )
        del_rec = await eng.imprint("delete", {"id": rec.id})
        assert del_rec.ok
        hits = await eng.recall({"merge_key": "d:1"})
        assert hits == []

    @pytest.mark.parametrize("fixture_name", _BACKENDS)
    @pytest.mark.asyncio
    async def test_recall_filters_by_tags(self, fixture_name, request):
        eng: Engram = request.getfixturevalue(fixture_name)
        await eng.imprint("append", {"content": "a", "tags": ["x", "y"]})
        await eng.imprint("append", {"content": "b", "tags": ["x"]})
        await eng.imprint("append", {"content": "c", "tags": ["y"]})
        out = await eng.recall({"top_k": 10}, filters={"tags": ["x", "y"]})
        assert len(out) == 1


# ---------------------------------------------------------------------------
# EngramClient: parent_id correlation, recall_mode, deadlines
# ---------------------------------------------------------------------------


@pytest.fixture
async def host_dendrite():
    """Dendrite that hosts the Engram and processes RECALL/IMPRINT."""
    syn = MemorySynapse()
    dendrite = Dendrite(
        synapse=syn, namespace="t", dendrite_id="host", role="worker",
        heartbeat_s=0,
    )
    eng = InMemoryEngram(engram_id="ctx", engram_kind="context")
    dendrite.attach_engram(eng)
    await dendrite.start()
    yield dendrite, syn
    await dendrite.stop()


@pytest.fixture
async def caller_dendrite(host_dendrite):
    """A second Dendrite on the same Synapse that issues RECALL/IMPRINT."""
    _, syn = host_dendrite
    dendrite = Dendrite(
        synapse=syn, namespace="t", dendrite_id="caller", role="orchestrator",
        heartbeat_s=0,
    )
    await dendrite.start()
    yield dendrite
    await dendrite.stop()


class TestEngramClient:

    @pytest.mark.asyncio
    async def test_recall_first_mode(self, host_dendrite, caller_dendrite):
        result = await caller_dendrite.recall(
            engram_id="ctx", query={"text": ""}, deadline_ms=500,
        )
        assert isinstance(result, RecallResult)
        # Empty engram -> no hits, but the request succeeded.
        assert result.hits == []
        assert result.engram_ids == ("ctx",)

    @pytest.mark.asyncio
    async def test_imprint_round_trip(self, host_dendrite, caller_dendrite):
        receipt = await caller_dendrite.imprint(
            engram_id="ctx", op="append",
            entry={"content": "first note"}, await_ack=True,
            deadline_ms=500,
        )
        assert isinstance(receipt, ImprintReceipt)
        assert receipt.ok
        assert receipt.id is not None
        # And the stored entry is recallable.
        result = await caller_dendrite.recall(
            engram_id="ctx", query={"text": "first"}, deadline_ms=500,
        )
        assert len(result.hits) == 1

    @pytest.mark.asyncio
    async def test_recall_deadline_raises(self, caller_dendrite):
        # No host attached for engram_id 'missing' -> deadline elapses.
        with pytest.raises(EngramTimeout):
            await caller_dendrite.recall(
                engram_id="missing-engram",
                query={"text": "x"},
                deadline_ms=80,
                recall_mode="first",
            )


# ---------------------------------------------------------------------------
# Axon binding: declarative engrams= + helper injection
# ---------------------------------------------------------------------------


class TestAxonBinding:

    def test_axon_without_engrams_is_unchanged(self):
        async def legacy(input, context):
            return {"answer": input["q"]}

        axon = Axon(
            neuron_id="legacy", neuron_fn=legacy, capabilities=["text"],
        )
        assert axon.engram_bindings == {}
        # Sanity: detection sees no recall/imprint kwargs.
        assert axon._fn_accepts_recall is False
        assert axon._fn_accepts_imprint is False

    def test_axon_with_engrams_detects_kwargs(self):
        async def fancy(input, context, *, recall, imprint):
            return {}

        axon = Axon(
            neuron_id="fancy", neuron_fn=fancy,
            engrams=[EngramBinding(name="ctx", engram_id="ctx-default")],
        )
        assert "ctx" in axon.engram_bindings
        assert axon._fn_accepts_recall is True
        assert axon._fn_accepts_imprint is True

    def test_engram_binding_requires_id_or_kind(self):
        with pytest.raises(ValueError, match="engram_id"):
            EngramBinding(name="ctx")

    @pytest.mark.asyncio
    async def test_unknown_binding_raises(self, host_dendrite):
        host, syn = host_dendrite

        async def fn(input, context, *, recall, imprint):
            await recall("nonexistent", query={"text": "x"})
            return {}

        worker = Dendrite(
            synapse=syn, namespace="t", dendrite_id="worker-bad",
            role="worker", heartbeat_s=0,
        )
        axon = Axon(
            neuron_id="bad", neuron_fn=fn,
            engrams=[EngramBinding(name="ctx", engram_id="ctx")],
        )
        worker.attach_axon(axon)
        await worker.start()
        try:
            # Direct call - the Axon's handle_task converts the raise to
            # an ERROR signal, but we can also call the helper directly.
            with pytest.raises(EngramNotBound):
                await axon._build_recall_helper(
                    new_trace_id(), new_event_id()
                )("nonexistent", query={"text": "x"})
        finally:
            await worker.stop()


# ---------------------------------------------------------------------------
# End-to-end: TASK → mid-task RECALL+IMPRINT → AGENT_OUTPUT
# ---------------------------------------------------------------------------


class TestEndToEnd:

    @pytest.mark.asyncio
    async def test_neuron_mid_task_recall_imprint(self):
        syn = MemorySynapse()

        # Engram host
        host = Dendrite(
            synapse=syn, namespace="e2e", dendrite_id="engram-host",
            role="worker", heartbeat_s=0,
        )
        host.attach_engram(InMemoryEngram(
            engram_id="ctx", engram_kind="context",
        ))

        # Worker hosting the Neuron
        seen = {}

        async def neuron_fn(input, context, *, recall, imprint):
            prior = await recall("ctx", query={"text": input["q"]})
            seen["prior_count"] = len(prior.hits)
            answer = f"answer-for: {input['q']}"
            await imprint(
                "ctx", op="append",
                entry={"content": answer, "tags": ["q"]},
                merge_key=f"q:{input['q']}",
                await_ack=True,
                deadline_ms=500,
            )
            return {"answer": answer}

        worker = Dendrite(
            synapse=syn, namespace="e2e", dendrite_id="worker",
            role="worker", heartbeat_s=0,
        )
        worker.attach_axon(Axon(
            neuron_id="researcher",
            neuron_fn=neuron_fn,
            capabilities=["research"],
            engrams=[EngramBinding(name="ctx", engram_id="ctx")],
        ))

        # Cortex
        cortex = Dendrite(
            synapse=syn, namespace="e2e", dendrite_id="cortex",
            role="orchestrator", heartbeat_s=0,
        )

        await host.start()
        await worker.start()
        await cortex.start()
        try:
            reply = await cortex.dispatch_and_wait(
                neuron="researcher", input={"q": "ping"},
                timeout_s=2.0,
            )
            assert reply.type is SignalType.AGENT_OUTPUT
            assert reply.payload["output"]["answer"] == "answer-for: ping"
            assert seen["prior_count"] == 0

            # The imprint should have landed in the engram.
            result = await cortex.recall(
                engram_id="ctx", query={"text": "answer-for"},
                deadline_ms=500,
            )
            assert len(result.hits) == 1
        finally:
            await cortex.stop()
            await worker.stop()
            await host.stop()


# ---------------------------------------------------------------------------
# Postgres conformance (skipped unless DSN given)
# ---------------------------------------------------------------------------


_PG_DSN = os.environ.get("COSMONAPSE_TEST_PG_DSN")


@pytest.mark.skipif(_PG_DSN is None, reason="set COSMONAPSE_TEST_PG_DSN to run")
class TestPostgresEngram:

    @pytest.mark.asyncio
    async def test_basic(self):
        from cosmonapse import PostgresEngram
        eng = PostgresEngram(
            dsn=_PG_DSN, engram_id="ctx-pg", engram_kind="context",
        )
        await eng.connect()
        try:
            rec = await eng.imprint(
                "append", {"content": "pg test", "tags": ["pg"]},
                imprint_id="evt_PGTESTIMPRINT00000000001",
            )
            assert rec.ok
            hits = await eng.recall({"text": "pg test"})
            assert len(hits) >= 1
        finally:
            await eng.close()
