"""
examples/building_a_neuron/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The shortest possible end-to-end Cosmonapse program, with a real LLM Neuron
backed by Hugging Face. One Neuron, one Axon, one Dendrite, one TASK, one
reply, in a single process against an in-memory Synapse.

The Neuron itself is *not* a hand-written function -- it's the unified
`Neuron(source="huggingface", ...)` factory. The Axon doesn't know it's
talking to an LLM; it would attach the same way to a Flask app, an MCP
server, or a plain async function.

What this example shows:

  1. Neuron     -- `Neuron(source="huggingface", ...)` returns an async
                   callable that satisfies `NeuronFn`. The Axon takes any
                   callable with the right shape.
  2. Axon       -- wraps the Neuron, gives it an id and capabilities, and
                   turns its `{"response": ...}` output into a valid
                   AGENT_OUTPUT Signal.
  3. Dendrite   -- the only component that touches the Synapse. Hosts the
                   Axon; emits REGISTER / HEARTBEAT / DEREGISTER; routes
                   inbound TASKs; exposes dispatch.
  4. Pathway    -- what dispatch_and_wait(...) is built on. The handle for
                   one logical workflow (one trace_id).
  5. Signal     -- the envelope that crosses the Synapse.

Setup:

    pip install cosmonapse httpx
    export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxx   # read scope is enough
    python examples/building_a_neuron/main.py

The HuggingFace token grants access to the Inference Providers router at
https://router.huggingface.co. Any open-weights chat model works; we use
Llama-3.1-8B-Instruct as the default.
"""
from __future__ import annotations

import asyncio
import os

from cosmonapse import (
    Axon,
    Dendrite,
    MemorySynapse,
    Neuron,
)


# ---------------------------------------------------------------------------
# 1. The Neuron -- backed by HuggingFace.
#
# `Neuron(source="huggingface", ...)` returns an async callable with the
# same signature every Neuron has: `async fn(input, context) -> output`.
# The Axon stores it, validates its output, and turns the return value
# into a protocol-valid AGENT_OUTPUT Signal -- exactly as it would for a
# plain Python function.
#
# Input the orchestrator sends: {"prompt": "..."} or {"messages": [...]}.
# Output the Neuron returns:    {"response": "<text>", "meta": <raw>}.
# ---------------------------------------------------------------------------
greeter = Neuron(
    source="huggingface",
    endpoint="https://router.huggingface.co",
    model="meta-llama/Llama-3.1-8B-Instruct",
    api_key=os.environ["HF_TOKEN"],
    use_chat_api=True,
    max_new_tokens=128,
    temperature=0.7,
)


async def main() -> None:
    # -----------------------------------------------------------------------
    # 2. The Synapse -- the message transport.
    #
    # MemorySynapse is in-process: it never opens a socket. Production uses
    # NatsSynapse / KafkaSynapse / DevSynapse instead, but the SDK surface
    # is identical -- the only change is the URL you connect with.
    # -----------------------------------------------------------------------
    synapse = MemorySynapse()
    await synapse.connect()

    try:
        # -------------------------------------------------------------------
        # 3. The Axon -- declares identity + capabilities, owns the Neuron.
        # -------------------------------------------------------------------
        axon = Axon(
            neuron_id="greeter",
            neuron_fn=greeter,
            capabilities=["text-generation", "chat", "greet"],
        )

        # -------------------------------------------------------------------
        # 4a. The worker Dendrite -- hosts the Axon, replies to TASKs.
        # -------------------------------------------------------------------
        worker = Dendrite(
            synapse=synapse,
            namespace="demo",
            role="worker",
        )
        worker.attach_axon(axon)

        # -------------------------------------------------------------------
        # 4b. The orchestrator Dendrite -- dispatches TASKs, collects replies.
        # -------------------------------------------------------------------
        orchestrator = Dendrite(
            synapse=synapse,
            namespace="demo",
        )

        # -------------------------------------------------------------------
        # 5. Run.
        #
        # `dispatch_and_wait` is sugar: emit a TASK, open a Pathway for the
        # new trace, await the first terminal Signal (AGENT_OUTPUT here),
        # close the Pathway, return the Signal.
        # -------------------------------------------------------------------
        async with worker, orchestrator:
            reply = await orchestrator.dispatch_and_wait(
                neuron="greeter",
                input={"prompt": "Say hello to a project called Cosmonapse in one line."},
                timeout_s=30.0,
            )
            print(f"[{reply.type.value}] {reply.payload['output']['response']}")
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
