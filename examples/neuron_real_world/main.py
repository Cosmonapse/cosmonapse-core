"""
examples/neuron_real_world/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A Neuron is *anything that interacts with the real world*  -  an MCP server, an
LLM, a plain async function. But an **HTTP API is not a Neuron**. Rather than
wrap a web app behind an Axon, you keep your web framework on the *outside* as
an HTTP boundary and dispatch TASK Signals from inside its route handlers,
using the orchestrator Dendrite's decorators directly in the Flask app.

This single-file example wires:

  * a **worker** Dendrite (``role="worker"``) hosting two real Neurons  - 
        - ``summary`` : a plain async function,
        - ``files``   : the standard filesystem MCP server, wrapped as a Neuron;
  * a **Flask app** that owns an **orchestrator** Dendrite (``role=
    "orchestrator"``). Its routes dispatch TASKs to those workers and block
    until the matching AGENT_OUTPUT comes back.

Flask is synchronous and Cosmonapse is async, so the orchestrator Dendrite runs
on an asyncio loop in a background thread; each Flask route and the asyncio
``@orch.on_agent_output`` handler hand off through a
``concurrent.futures.Future`` keyed by ``trace_id``.

Run with MemorySynapse (no external broker):

    pip install flask mcp        # soft deps for the sources used here
    python examples/neuron_real_world/main.py

    # then, in another terminal:
    curl -s -X POST localhost:5000/summarise \\
         -H 'Content-Type: application/json' \\
         -d '{"text": "Cosmonapse keeps your API at the edge."}'
    curl -s -X POST localhost:5000/files
"""

import asyncio
import concurrent.futures
import threading

from flask import Flask, jsonify, request

from cosmonapse import (
    Axon,
    Dendrite,
    MemorySynapse,
    Neuron,
    new_trace_id,
)

NAMESPACE = "demo"
FLASK_PORT = 5000


# ---------------------------------------------------------------------------
# 1.  Real-world Neurons  -  the worker side
# ---------------------------------------------------------------------------
# A Neuron is a plain ``async (input, context) -> dict`` callable. Neither of
# these knows anything about HTTP, Flask, or the protocol.

async def summary_neuron(input: dict, context: list) -> dict:
    text = input.get("text", "")
    return {"summary": text[:120], "length": len(text)}


# The standard filesystem MCP server, wrapped as a Neuron (wrapper only  -  we
# don't ship the server; ``.`` is the allowed directory).
files_neuron = Neuron(
    source="mcp",
    server="filesystem",
    args=["."],
    tool="list_directory",
)


# ---------------------------------------------------------------------------
# 2.  The async runtime  -  a background asyncio loop hosting both Dendrites
# ---------------------------------------------------------------------------

_loop = asyncio.new_event_loop()
_pending: dict[str, "concurrent.futures.Future[dict]"] = {}
_orch: Dendrite | None = None


def _run_loop() -> None:
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


threading.Thread(target=_run_loop, daemon=True, name="cosmo-loop").start()


async def _setup() -> None:
    global _orch
    synapse = MemorySynapse()
    await synapse.connect()

    # Worker Dendrite: hosts the Axons, replies to TASKs, never dispatches.
    worker = Dendrite(synapse=synapse, namespace=NAMESPACE,
                      dendrite_id="workers", role="worker")
    worker.attach_axon(Axon(neuron_id="summary", neuron_fn=summary_neuron,
                            capabilities=["summarise"]))
    worker.attach_axon(Axon(neuron_id="files", neuron_fn=files_neuron,
                            capabilities=["mcp", "filesyste