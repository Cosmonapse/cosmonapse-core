"""
examples/neuron_real_world/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A Neuron is *anything that interacts with the real world*. This example wires
three different kinds of Neuron onto the same Synapse, all behind the identical
``Axon`` interface — the rest of the protocol can't tell them apart:

  * an **API**         — an existing Flask app, served in-process,
  * an **MCP server**  — the standard filesystem server, wrapped as a Neuron,
  * an **LLM**         — an Ollama model (commented out; needs a daemon).

Run with MemorySynapse (no external broker):

    pip install httpx mcp flask        # soft deps for the sources used here
    python examples/neuron_real_world/main.py
"""

import asyncio

from flask import Flask, jsonify, request

from cosmonapse import (
    Axon,
    Dendrite,
    MemoryRegistryStore,
    MemorySynapse,
    Neuron,
)

# ---------------------------------------------------------------------------
# 1.  An ordinary Flask API → a Neuron
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.post("/summarise")
def summarise():
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    return jsonify(summary=text[:120], length=len(text))


api_neuron = Neuron(source="flask", app=app, default_path="/summarise")

# ---------------------------------------------------------------------------
# 2.  A standard MCP server → a Neuron (wrapper only; we don't ship the server)
# ---------------------------------------------------------------------------
# Spawns `npx -y @modelcontextprotocol/server-filesystem .` over stdio and
# exposes its tools. `tool="list_directory"` is the default tool to call.

fs_neuron = Neuron(
    source="mcp",
    server="filesystem",
    args=["."],               # allowed directory
    tool="list_directory",
)

# ---------------------------------------------------------------------------
# 3.  (Optional) an LLM → a Neuron
# ---------------------------------------------------------------------------
# llm_neuron = Neuron(source="ollama", model="llama3")


async def main():
    synapse = MemorySynapse()
    store = MemoryRegistryStore()

    worker = Dendrite(synapse=synapse, namespace="demo")
    worker.attach_axon(Axon(neuron_id="summary-api", neuron_fn=api_neuron,
                            capabilities=["http", "summarise"]))
    worker.attach_axon(Axon(neuron_id="files", neuron_fn=fs_neuron,
                            capabilities=["mcp", "filesystem"]))

    orch = Dendrite(synapse=synapse, registry_store=store, namespace="demo")

    seen: dict = {}

    @orch.on_agent_output
    async def on_output(sig):
        seen[sig.payload["neuron"]] = sig.payload["output"]
        await orch.emit_final(trace_id=sig.trace_id, parent_id=sig.id,
                              result=sig.payload["output"])

    async with orch, worker:
        # Hit the Flask API neuron. The whole input becomes the JSON body
        # because it carries a `text` key and no explicit method/path.
        await orch.dispatch_task(
            neuron="summary-api",
            input={"text": "Cosmonapse turns any real-world thing into a neuron."},
        )
        # Ask the filesystem MCP server to list the current directory.
        await orch.dispatch_task(
            neuron="files",
            input={"tool": "list_directory", "arguments": {"path": "."}},
        )
        await asyncio.sleep(5)

    print("API neuron  :", seen.get("summary-api"))
    print("MCP neuron  :", seen.get("files"))


if __name__ == "__main__":
    asyncio.run(main())
