"""
Per-operation Pathway correlation by ``parent_id`` - the primitive that lets
EngramClient (and future request/reply clients) be thin wrappers over a
Pathway instead of bespoke Future tables.

Covers:
  * _open_op_pathway delivers an inbound Signal to the op-Pathway whose
    op_id == signal.parent_id (request/reply correlation)
  * a non-matching parent_id on the SAME trace is not delivered (concurrent
    ops on one trace don't cross-talk - the demux a trace-Pathway lacks)
  * a FINAL on the trace closes in-flight op-Pathways and evicts them, so an
    awaiting recall/imprint wakes (surfaced as EngramCancelled by the client)
"""

import asyncio

import pytest

from cosmonapse import (
    Dendrite,
    MemorySynapse,
    SignalType,
    new_event_id,
    new_trace_id,
)
from cosmonapse.envelope import Directed, final_signal, recalled_signal


def _run(coro):
    return asyncio.run(coro)


async def _synapse():
    s = MemorySynapse()
    await s.connect()
    return s


def test_op_pathway_correlates_by_parent_id():
    async def run():
        s = await _synapse()
        d = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with d:
                tid = new_trace_id()
                op_id = new_event_id()
                pw = d._open_op_pathway(op_id=op_id, trace_id=tid)
                await d._dispatch_inbound(recalled_signal(
                    trace_id=tid, parent_id=op_id,
                    engram_id="eng_a",
                    hits=[{"id": "h1", "entry": {"v": 1}, "score": 0.9}],
                    directed=Directed(id="caller"),
                ))
                got = await pw.wait_for(SignalType.RECALLED, timeout_s=1.0)
                assert got.parent_id == op_id
                assert got.payload["engram_id"] == "eng_a"
                await pw.close()
                assert op_id not in d._op_pathways
        finally:
            await s.close()
    _run(run())


def test_op_pathway_ignores_other_parent_id_same_trace():
    async def run():
        s = await _synapse()
        d = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with d:
                tid = new_trace_id()
                pw = d._open_op_pathway(op_id=new_event_id(), trace_id=tid)
                # Same trace, a DIFFERENT op id: must not reach this op-Pathway.
                await d._dispatch_inbound(recalled_signal(
                    trace_id=tid, parent_id=new_event_id(),
                    engram_id="eng_a", hits=[],
                    directed=Directed(id="caller"),
                ))
                with pytest.raises(asyncio.TimeoutError):
                    await pw.wait_for(SignalType.RECALLED, timeout_s=0.1)
                await pw.close()
        finally:
            await s.close()
    _run(run())


def test_op_pathway_closed_on_trace_terminal():
    async def run():
        s = await _synapse()
        d = Dendrite(synapse=s, namespace="t", heartbeat_s=0)
        try:
            async with d:
                tid = new_trace_id()
                op_id = new_event_id()
                pw = d._open_op_pathway(op_id=op_id, trace_id=tid)
                await d._dispatch_inbound(final_signal(
                    trace_id=tid, parent_id=new_event_id(),
                    directed=Directed(id="orch"), result={},
                ))
                assert pw.closed
                assert op_id not in d._op_pathways
        finally:
            await s.close()
    _run(run())
