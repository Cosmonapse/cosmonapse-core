"""
PERMISSION is a *pause* like CLARIFICATION: it must wake a waiting consumer
(``wait()`` / ``dispatch_and_wait``) and be delivered under ``scope="terminal"``
so a permission request can't be stranded - but it must NOT auto-close the
Pathway, because the workflow resumes once the decision arrives.
"""

import asyncio

from cosmonapse import Pathway, SignalType
from cosmonapse.envelope import (
    Directed,
    agent_output_signal,
    new_event_id,
    new_trace_id,
    permission_signal,
)


def _run(coro):
    return asyncio.run(coro)


def _perm(tid):
    return permission_signal(
        trace_id=tid, parent_id=new_event_id(),
        directed=Directed(id="worker"),
        action="delete", reason="needs approval",
    )


def test_permission_wakes_bare_wait():
    async def run():
        tid = new_trace_id()
        pw = Pathway(trace_id=tid)
        await pw._deliver(_perm(tid))
        got = await pw.wait(timeout_s=1.0)
        assert got.type is SignalType.PERMISSION
        assert not pw.closed  # a pause, not a terminal event
        await pw.close()
    _run(run())


def test_permission_delivered_under_scope_terminal():
    async def run():
        tid = new_trace_id()
        pw = Pathway(trace_id=tid, scope="terminal")
        # Intermediate AGENT_OUTPUT is dropped by scope="terminal"...
        await pw._deliver(agent_output_signal(
            trace_id=tid, parent_id=new_event_id(),
            directed=Directed(id="worker"), output={"v": 1},
        ))
        # ...but a PERMISSION request is a needed decision: delivered + wakes.
        await pw._deliver(_perm(tid))
        got = await pw.wait(timeout_s=1.0)
        assert got.type is SignalType.PERMISSION
        assert not pw.closed
        await pw.close()
    _run(run())
